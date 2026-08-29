"""Poll a Douyin creator's public works and send SMTP notifications.

The public Douyin web page currently generates request signatures in the page
runtime.  The monitor therefore uses Playwright to load the creator page and
calls the same-origin works endpoint from that page.  This keeps the workflow
free of hard-coded signature algorithms that change frequently.

The state file is intentionally small and is committed by the GitHub Actions
workflow after a successful check.  A first run creates a baseline and does
not send an email unless NOTIFY_ON_FIRST_RUN is enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright


LOGGER = logging.getLogger("douyin-monitor")

DEFAULT_USER_URL = (
    "https://www.douyin.com/user/"
    "MS4wLjABAAAA3Z3BGF5DOu1M-ONu57cXLA7uGZmQI8ibm_ZVx_837Ao?from_tab_name=main"
)
DEFAULT_STATE_FILE = Path(__file__).with_name("state.json")
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class MonitorError(RuntimeError):
    """An expected, user-actionable monitor error."""


class DouyinFetchError(MonitorError):
    """The Douyin page or works endpoint could not be read."""


@dataclass(frozen=True)
class Work:
    """The small subset of a Douyin work needed by an email notification."""

    aweme_id: str
    description: str
    create_time: int
    url: str
    nickname: str
    is_top: bool = False
    digg_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None


@dataclass(frozen=True)
class MonitorConfig:
    user_url: str
    sec_user_id: str
    state_file: Path
    max_items: int
    max_seen: int
    page_wait_ms: int
    navigation_timeout_ms: int
    fetch_retries: int
    notify_on_first_run: bool
    published_timezone: str
    douyin_cookie: str


def env_text(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value if value else default


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = env_text(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise MonitorError(f"环境变量 {name} 必须是整数，当前值为 {raw!r}") from exc
    if value < minimum:
        raise MonitorError(f"环境变量 {name} 必须大于等于 {minimum}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_text(name)
    if not raw:
        return default
    if raw.lower() in {"1", "true", "yes", "y", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "n", "off"}:
        return False
    raise MonitorError(f"环境变量 {name} 必须是 true/false，当前值为 {raw!r}")


def extract_sec_user_id(user_url: str) -> str:
    """Extract and validate the sec_user_id from a Douyin profile URL."""

    parsed = urlparse(user_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MonitorError("DOUYIN_USER_URL 必须是完整的 http(s) 抖音主页 URL")

    match = re.search(r"/user/([^/?#]+)", parsed.path)
    if not match:
        raise MonitorError("DOUYIN_USER_URL 中没有找到 /user/<sec_user_id>")

    sec_user_id = unquote(match.group(1)).strip()
    if len(sec_user_id) < 20 or not re.fullmatch(r"[A-Za-z0-9_-]+", sec_user_id):
        raise MonitorError("抖音主页 URL 中的 sec_user_id 格式不正确")
    return sec_user_id


def build_config(args: argparse.Namespace) -> MonitorConfig:
    user_url = args.user_url or env_text("DOUYIN_USER_URL", DEFAULT_USER_URL)
    sec_user_id = env_text("DOUYIN_SEC_USER_ID") or extract_sec_user_id(user_url)
    raw_state_file = args.state_file or env_text("DOUYIN_STATE_FILE")
    state_file = Path(raw_state_file) if raw_state_file else DEFAULT_STATE_FILE
    if not state_file.is_absolute():
        state_file = Path.cwd() / state_file

    return MonitorConfig(
        user_url=user_url,
        sec_user_id=sec_user_id,
        state_file=state_file,
        max_items=env_int("DOUYIN_MAX_ITEMS", 30, minimum=1),
        max_seen=env_int("DOUYIN_MAX_SEEN", 500, minimum=20),
        page_wait_ms=env_int("DOUYIN_PAGE_WAIT_MS", 4000, minimum=0),
        navigation_timeout_ms=env_int("DOUYIN_NAVIGATION_TIMEOUT_MS", 60000, minimum=1000),
        fetch_retries=env_int("DOUYIN_FETCH_RETRIES", 2, minimum=1),
        notify_on_first_run=env_bool("NOTIFY_ON_FIRST_RUN", False),
        published_timezone=env_text("PUBLISHED_TIMEZONE", DEFAULT_TIMEZONE),
        douyin_cookie=env_text("DOUYIN_COOKIE"),
    )


def parse_cookie_header(cookie_header: str) -> list[dict[str, str]]:
    """Convert a browser Cookie header into Playwright cookies."""

    if not cookie_header:
        return []

    cookies: list[dict[str, str]] = []
    parsed = SimpleCookie()
    try:
        parsed.load(cookie_header)
    except Exception:
        parsed = SimpleCookie()

    if parsed:
        for name, morsel in parsed.items():
            if name and morsel.value:
                cookies.append(
                    {
                        "name": name,
                        "value": morsel.value,
                        "domain": ".douyin.com",
                        "path": "/",
                    }
                )
        return cookies

    # SimpleCookie is deliberately strict.  This fallback handles copied
    # Cookie headers containing a value that SimpleCookie does not accept.
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if name and separator and value:
            cookies.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".douyin.com",
                    "path": "/",
                }
            )
    return cookies


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_work(raw: dict[str, Any]) -> Work | None:
    aweme_id = str(raw.get("aweme_id") or "").strip()
    if not aweme_id:
        return None
    return Work(
        aweme_id=aweme_id,
        description=str(raw.get("description") or "").strip(),
        create_time=_safe_int(raw.get("create_time")) or 0,
        url=str(raw.get("url") or f"https://www.douyin.com/video/{aweme_id}"),
        nickname=str(raw.get("nickname") or "抖音博主").strip() or "抖音博主",
        is_top=bool(raw.get("is_top")),
        digg_count=_safe_int(raw.get("digg_count")),
        comment_count=_safe_int(raw.get("comment_count")),
        share_count=_safe_int(raw.get("share_count")),
    )


# This function runs in the Douyin page.  Calling fetch from this page lets
# Douyin's own runtime add its current dynamic query parameters/signatures.
FETCH_WORKS_IN_PAGE = r"""
async ({ secUserId, count }) => {
  const endpoint = new URL('/aweme/v1/web/aweme/post/', location.origin);
  const params = {
    device_platform: 'webapp',
    aid: '6383',
    channel: 'channel_pc_web',
    sec_user_id: secUserId,
    max_cursor: '0',
    count: String(count),
    locate_query: 'false',
    show_live_replay_strategy: '1',
    need_time_list: '1',
    time_list_query: '0',
    whale_cut_token: '',
    cut_version: '1',
    publish_video_strategy_type: '2',
    from_user_page: '1',
    update_version_code: '170400',
    pc_client_type: '1'
  };
  for (const [key, value] of Object.entries(params)) {
    endpoint.searchParams.set(key, value);
  }

  const response = await fetch(endpoint.toString(), {
    credentials: 'include',
    headers: { accept: 'application/json, text/plain, */*' }
  });
  const text = await response.text();
  if (!text.trim()) {
    return { http_status: response.status, response_url: response.url, body: '' };
  }

  let payload;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    return {
      http_status: response.status,
      response_url: response.url,
      body: text.slice(0, 500),
      parse_error: String(error)
    };
  }

  const list = Array.isArray(payload.aweme_list) ? payload.aweme_list : [];
  const items = list.map(item => {
    // Prefer *_str fields: a 19-digit aweme_id must not pass through a JS
    // number, otherwise its precision could be lost before Python sees it.
    const awemeId = String(item.aweme_id_str || item.aweme_id || '');
    if (!awemeId) return null;
    const statistics = item.statistics || {};
    const shareUrl = item.share_info && item.share_info.share_url;
    return {
      aweme_id: awemeId,
      description: String(item.desc || ''),
      create_time: Number(item.create_time || 0),
      url: typeof shareUrl === 'string' && shareUrl.includes('/video/')
        ? shareUrl
        : `https://www.douyin.com/video/${awemeId}`,
      nickname: String((item.author && item.author.nickname) || ''),
      is_top: Boolean(item.is_top || item.is_pinned || item.aweme_control?.is_top),
      digg_count: statistics.digg_count,
      comment_count: statistics.comment_count,
      share_count: statistics.share_count
    };
  }).filter(Boolean);

  return {
    http_status: response.status,
    response_url: response.url,
    status_code: payload.status_code,
    max_cursor: payload.max_cursor,
    has_more: payload.has_more,
    items
  };
}
"""


async def fetch_works_once(config: MonitorConfig) -> list[Work]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            context: BrowserContext = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                user_agent=DEFAULT_USER_AGENT,
            )
            try:
                if config.douyin_cookie:
                    cookies = parse_cookie_header(config.douyin_cookie)
                    if cookies:
                        await context.add_cookies(cookies)
                        LOGGER.info("已加载 DOUYIN_COOKIE 中的 %d 个 Cookie。", len(cookies))

                page: Page = await context.new_page()
                response = await page.goto(
                    config.user_url,
                    wait_until="domcontentloaded",
                    timeout=config.navigation_timeout_ms,
                )
                LOGGER.info(
                    "主页已打开：HTTP %s，等待页面运行时 %dms。",
                    response.status if response else "unknown",
                    config.page_wait_ms,
                )
                if config.page_wait_ms:
                    await page.wait_for_timeout(config.page_wait_ms)

                result = await page.evaluate(
                    FETCH_WORKS_IN_PAGE,
                    {"secUserId": config.sec_user_id, "count": config.max_items},
                )
                if not isinstance(result, dict):
                    raise DouyinFetchError("抖音作品接口返回了无法识别的数据")

                http_status = result.get("http_status")
                body = str(result.get("body") or "").strip()
                if not body and "items" not in result:
                    raise DouyinFetchError(
                        "抖音作品接口返回空响应；请稍后重试，或配置最新的 DOUYIN_COOKIE"
                    )
                if result.get("parse_error"):
                    raise DouyinFetchError("抖音作品接口返回的内容不是有效 JSON")
                if http_status != 200:
                    raise DouyinFetchError(f"抖音作品接口 HTTP 状态异常：{http_status}")
                if result.get("status_code") not in (None, 0, "0"):
                    raise DouyinFetchError(
                        f"抖音作品接口返回 status_code={result.get('status_code')}"
                    )

                works: list[Work] = []
                for raw in result.get("items") or []:
                    if isinstance(raw, dict):
                        work = normalize_work(raw)
                        if work:
                            works.append(work)
                LOGGER.info("本次读取到 %d 个作品。", len(works))
                return works
            finally:
                await context.close()
        finally:
            await browser.close()


async def fetch_works(config: MonitorConfig) -> list[Work]:
    last_error: Exception | None = None
    for attempt in range(1, config.fetch_retries + 1):
        try:
            return await fetch_works_once(config)
        except Exception as exc:  # retry browser/network failures as one unit
            last_error = exc
            LOGGER.warning("第 %d/%d 次抓取失败：%s", attempt, config.fetch_retries, exc)
            if attempt < config.fetch_retries:
                await asyncio.sleep(min(10, attempt * 3))
    raise MonitorError(f"连续 {config.fetch_retries} 次抓取抖音失败：{last_error}") from last_error


def load_state(path: Path, sec_user_id: str) -> tuple[bool, list[str]]:
    if not path.exists():
        return False, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"无法读取状态文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MonitorError(f"状态文件 {path} 不是 JSON 对象")

    saved_sec_user_id = str(data.get("sec_user_id") or "").strip()
    if saved_sec_user_id and saved_sec_user_id != sec_user_id:
        raise MonitorError(
            f"状态文件属于 sec_user_id={saved_sec_user_id}，当前配置为 {sec_user_id}；"
            "请删除/重命名 douyin/state.json 后重新建立基线"
        )

    raw_seen = data.get("seen_ids") or []
    if not isinstance(raw_seen, list):
        raise MonitorError(f"状态文件 {path} 的 seen_ids 必须是数组")
    seen_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in raw_seen
            if isinstance(item, (str, int)) and str(item).strip()
        )
    )
    initialized = bool(data.get("initialized"))
    return initialized, seen_ids


def save_state(path: Path, sec_user_id: str, seen_ids: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "sec_user_id": sec_user_id,
        "initialized": True,
        "seen_ids": list(dict.fromkeys(seen_ids)),
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def format_publish_time(timestamp: int, timezone_name: str) -> str:
    if not timestamp:
        return "发布时间未知"
    try:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(timezone_name)
        except Exception:
            LOGGER.warning("时区 %r 不可用，将使用 UTC。", timezone_name)
            tz = timezone.utc
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(tz).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    except (OverflowError, OSError, ValueError):
        return "发布时间未知"


def clean_header_text(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value).strip()


def display_description(work: Work) -> str:
    return work.description or "（该作品没有文字描述）"


def render_email(
    works: list[Work],
    *,
    timezone_name: str,
) -> tuple[str, str, str]:
    nickname = clean_header_text(works[0].nickname if works else "抖音博主")
    count = len(works)
    subject = f"[抖音更新] {nickname} 发布了 {count} 个新作品"

    text_lines = [f"抖音博主「{nickname}」有 {count} 个新作品：", ""]
    html_items: list[str] = []
    for index, work in enumerate(
        sorted(works, key=lambda item: (item.create_time, item.aweme_id), reverse=True),
        start=1,
    ):
        published = format_publish_time(work.create_time, timezone_name)
        description = display_description(work)
        text_lines.extend(
            [
                f"{index}. {description}",
                f"   发布时间：{published}",
                f"   链接：{work.url}",
            ]
        )
        stats: list[str] = []
        if work.digg_count is not None:
            stats.append(f"点赞 {work.digg_count}")
        if work.comment_count is not None:
            stats.append(f"评论 {work.comment_count}")
        if work.share_count is not None:
            stats.append(f"分享 {work.share_count}")
        if stats:
            text_lines.append(f"   {'，'.join(stats)}")
        text_lines.append("")

        stats_html = f"<p>{html.escape('，'.join(stats))}</p>" if stats else ""
        top_badge = " <em>置顶</em>" if work.is_top else ""
        html_items.append(
            "<li>"
            f"<a href=\"{html.escape(work.url, quote=True)}\">"
            f"{html.escape(description)}"
            "</a>"
            f"{top_badge}"
            f"<p>发布时间：{html.escape(published)}</p>"
            f"{stats_html}"
            "</li>"
        )

    text_body = "\n".join(text_lines).rstrip() + "\n"
    html_body = (
        "<!doctype html><html><body>"
        f"<p>抖音博主「{html.escape(nickname)}」有 {count} 个新作品：</p>"
        f"<ol>{''.join(html_items)}</ol>"
        "<p>此邮件由 GitHub Actions 自动发送。</p>"
        "</body></html>"
    )
    return subject, text_body, html_body


def split_recipients(raw: str) -> list[str]:
    recipients = [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]
    if not recipients:
        raise MonitorError("请配置 SMTP_TO 收件邮箱")
    return recipients


def send_email(works: list[Work], config: MonitorConfig) -> None:
    username = env_text("SMTP_USERNAME") or env_text("SMTP_FROM")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_address = env_text("SMTP_FROM") or username
    to_addresses = split_recipients(env_text("SMTP_TO"))
    host = env_text("SMTP_HOST", "smtp.qq.com")
    use_ssl = env_bool("SMTP_USE_SSL", True)
    starttls = env_bool("SMTP_STARTTLS", not use_ssl)
    default_port = 465 if use_ssl else 587
    port = env_int("SMTP_PORT", default_port, minimum=1)
    timeout = env_int("SMTP_TIMEOUT_SECONDS", 30, minimum=1)

    if not username:
        raise MonitorError("请配置 SMTP_USERNAME 或 SMTP_FROM")
    if not password:
        raise MonitorError("请配置 SMTP_PASSWORD（QQ/163 通常填写 SMTP 授权码）")
    if not from_address:
        raise MonitorError("请配置 SMTP_FROM")

    subject, text_body, html_body = render_email(
        works,
        timezone_name=config.published_timezone,
    )
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = ", ".join(to_addresses)
    message["Subject"] = clean_header_text(subject)
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    LOGGER.info("正在通过 %s:%d 发送通知到 %s。", host, port, ", ".join(to_addresses))
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context()) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            if starttls:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.login(username, password)
            server.send_message(message)
    LOGGER.info("邮件发送成功。")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查抖音博主新作品并发送 SMTP 邮件")
    parser.add_argument("--user-url", help="覆盖 DOUYIN_USER_URL")
    parser.add_argument("--state-file", help="覆盖 DOUYIN_STATE_FILE")
    parser.add_argument(
        "--notify-on-first-run",
        action="store_true",
        help="首次运行也发送当前作品（默认只建立基线）",
    )
    return parser.parse_args()


async def run(config: MonitorConfig) -> int:
    initialized, seen_ids = load_state(config.state_file, config.sec_user_id)
    works = await fetch_works(config)
    current_ids = [work.aweme_id for work in works]
    new_works = [work for work in works if work.aweme_id not in set(seen_ids)]
    notify_on_first = config.notify_on_first_run

    if not initialized:
        if notify_on_first and new_works:
            send_email(new_works, config)
        merged_ids = (current_ids + seen_ids)[: config.max_seen]
        save_state(config.state_file, config.sec_user_id, merged_ids)
        if new_works and notify_on_first:
            LOGGER.info("首次运行已通知 %d 个当前作品，并建立基线。", len(new_works))
        else:
            LOGGER.info("首次运行仅建立基线，记录 %d 个作品，不发送历史作品通知。", len(current_ids))
        return 0

    if not new_works:
        LOGGER.info("没有发现新作品。")
        return 0

    # Only update state after SMTP succeeds.  A transient mail failure then
    # causes the same works to be retried on the next scheduled run.
    send_email(new_works, config)
    merged_ids = (current_ids + seen_ids)[: config.max_seen]
    save_state(config.state_file, config.sec_user_id, merged_ids)
    LOGGER.info("已通知 %d 个新作品并更新状态。", len(new_works))
    return 0


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, env_text("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = build_config(parse_args())
        LOGGER.info("开始检查 sec_user_id=%s。", config.sec_user_id)
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        LOGGER.error("任务被中断。")
        return 130
    except Exception as exc:
        LOGGER.error("任务失败：%s", exc)
        return 1


if __name__ == "__main__":
    # GitHub logs are UTF-8; this also makes local Windows runs readable when
    # the active console defaults to a legacy code page.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
