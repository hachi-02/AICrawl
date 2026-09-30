"""Các hàm chuẩn hóa thuần túy: URL, tiêu đề, thời gian, simhash.

Module này không phụ thuộc vào bất kỳ module nào khác của project.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
import unicodedata
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --------------------------------------------------------------------------
# URL
# --------------------------------------------------------------------------

#: Các query param không mang ý nghĩa về nội dung bài viết.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid", "gclid", "dclid", "msclkid", "twclid", "igshid", "mc_cid", "mc_eid",
        "ref", "referrer", "source", "src", "cmpid", "campaign", "campaign_id",
        "ito", "ns_campaign", "ns_mchannel", "ns_source", "at_medium", "at_campaign",
        "spm", "share", "shared", "s_cid", "cid", "trk", "trkCampaign", "sh",
        "_ga", "_gl", "yclid", "wickedid", "guccounter", "guce_referrer",
    }
)

_TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "pk_", "piwik_", "mtm_", "hsa_", "_hs")


def canonicalize_url(url: str) -> str:
    """Đưa URL về dạng chuẩn để so sánh trùng lặp.

    Bỏ query tracking, bỏ fragment, bỏ dấu ``/`` cuối, lowercase host.
    """
    if not url:
        return ""
    raw = url.strip()
    if not raw:
        return ""

    if not raw.startswith(("http://", "https://", "//")):
        raw = "https://" + raw.lstrip("/")

    parts = urlsplit(raw)
    original_scheme = parts.scheme.lower()
    scheme = "https" if original_scheme in ("http", "https", "") else original_scheme
    host = parts.netloc.lower()

    # Bỏ port mặc định
    if host.endswith(":80") and original_scheme == "http":
        host = host[:-3]
    if host.endswith(":443") and scheme == "https":
        host = host[:-4]
    if host.startswith("www."):
        host = host[4:]

    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in TRACKING_PARAMS
        and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(kept_pairs), doseq=True)

    path = re.sub(r"/amp/?$", "", path) or "/"
    return urlunsplit((scheme, host, path, query, ""))


def extract_host(url: str) -> str:
    """Trả về host của URL (không có www.)."""
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------
# Tiêu đề
# --------------------------------------------------------------------------

#: Hậu tố kiểu " - TechCrunch" / " | The Verge" do CMS tự thêm vào <title>.
#: Chỉ bỏ khi phần trước hậu tố có đủ nhiều từ, tránh cắt nhầm những tiêu đề
#: dạng "Café & Co. ..." thành "Café".
_TITLE_SUFFIX_RE = re.compile(r"\s+[\|–—·:>»-]\s+[^-–—|]{2,45}$")
_TITLE_MIN_WORDS_BEFORE_SUFFIX = 3
_TITLE_MAX_SUFFIX_WORDS = 4

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")

#: Từ dừng tiếng Anh thường gặp, bỏ đi để so sánh tiêu đề bớt nhiễu.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but of for to in on at by with from as is are was were be been being
    this that these those it its into over after before under above between during
    new news say says said will would can could should may might must about
    more most other some such than then there here their they them his her our your my
    you we i us our us not no nor only own same so too very just
    """.split()
)

_SHINGLE_SIZE = 3


def strip_html_entities(text: str) -> str:
    """Giải mã entity HTML (``&amp;`` ``&eacute;`` ``&#39;`` ``&nbsp;`` ...).

    Dùng :func:`html.unescape` để phủ toàn bộ named reference của HTML5, có
    chuẩn hoá Unicode sau đó vì nhiều CMS vẫn nhét entity dạng cũ vào tiêu đề.
    """
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    return text


def normalize_title(title: str) -> str:
    """Chuẩn hóa tiêu đề: bỏ entity, hậu tố nguồn, dấu câu, gọn whitespace, lowercase."""
    if not title:
        return ""
    text = strip_html_entities(title).replace("\u00a0", " ")
    text = _strip_source_suffix(text)
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _strip_source_suffix(text: str) -> str:
    """Bỏ đuôi " - TechCrunch" chỉ khi an toàn (phần chính đủ dài)."""
    match = _TITLE_SUFFIX_RE.search(text)
    if match is None:
        return text
    head, tail = text[: match.start()], match.group().strip(" \t|–—·:>»-")
    if len(head.split()) < _TITLE_MIN_WORDS_BEFORE_SUFFIX:
        return text
    if len(tail.split()) > _TITLE_MAX_SUFFIX_WORDS:
        return text
    return head


def tokenize(text: str) -> list[str]:
    """Tách tiêu đề đã chuẩn hóa thành danh sách token.

    Giữ lại token ngắn dạng số vì chúng rất quan trọng cho tin công nghệ
    ("GPT-5", "LLaMA 3"), nhưng bỏ chữ cái lẻ rác ("s", "t").
    """
    tokens: list[str] = []
    for token in normalize_title(text).split():
        if token in STOPWORDS:
            continue
        if len(token) >= 2 or token.isdigit():
            tokens.append(token)
    return tokens


def token_set(text: str) -> frozenset[str]:
    """Tập token của tiêu đề."""
    return frozenset(tokenize(text))


def jaccard(left: frozenset[str] | set[str], right: frozenset[str] | set[str]) -> float:
    """Độ tương đồng Jaccard giữa hai tập token (0.0 → 1.0)."""
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if not intersection:
        return 0.0
    return intersection / len(left | right)


def overlap_coefficient(left: frozenset[str] | set[str], right: frozenset[str] | set[str]) -> float:
    """Hệ số chồng lấn |A∩B| / min(|A|,|B|).

    Dùng cho so khớp **tiêu đề** giữa hai nguồn khác nhau: tiêu đề ngắn nên
    Jaccard quá nghiêm ngặt, còn hệ số chồng lấn phản ánh đúng việc hai nguồn
    viết lại cùng một sự kiện.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def ngram_shingles(tokens: list[str], size: int = _SHINGLE_SIZE) -> list[str]:
    """Tạo n-gram liên tiếp từ danh sách token."""
    if not tokens:
        return []
    if len(tokens) < size:
        return [" ".join(tokens)]
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


# --------------------------------------------------------------------------
# Simhash 64-bit
# --------------------------------------------------------------------------

_HASH_BITS = 64
_MASK64 = (1 << _HASH_BITS) - 1


def _feature_hash(feature: str) -> int:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash(features: list[str]) -> int:
    """Tính simhash 64-bit từ danh sách feature (shingle của tiêu đề + summary)."""
    if not features:
        return 0
    weights = [0] * _HASH_BITS
    for feature in features:
        value = _feature_hash(feature)
        for bit in range(_HASH_BITS):
            if value >> bit & 1:
                weights[bit] += 1
            else:
                weights[bit] -= 1
    fingerprint = 0
    for bit in range(_HASH_BITS):
        if weights[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming_distance(left: int, right: int) -> int:
    """Khoảng cách Hamming giữa hai simhash 64-bit."""
    return ((left ^ right) & _MASK64).bit_count()


def simhex(value: int) -> str:
    """Đổi simhash sang chuỗi hex 16 ký tự để lưu DB."""
    return f"{value & _MASK64:016x}"


def simint(value: str | int | None) -> int:
    """Đổi chuỗi hex trong DB về int (giá trị rỗng trả 0)."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value & _MASK64
    try:
        return int(str(value).strip(), 16) & _MASK64
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# Content hash
# --------------------------------------------------------------------------


def content_hash(title: str, source: str) -> str:
    """SHA256 của tiêu đề đã chuẩn hóa + tên nguồn.

    Dùng để chặn trùng tuyệt đối: cùng tiêu đề chuẩn hóa và cùng nguồn
    thì hash luôn giống nhau, bất kể URL có khác hay không.
    """
    key = f"{normalize_title(title)}|{source.strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def fingerprint_features(title: str, summary: str | None, content: str | None = None) -> list[str]:
    """Sinh feature cho simhash: shingle tiêu đề + 300 ký tự summary/content.

    Nhờ vậy hai bài có tiêu đề hơi khác nhau nhưng nội dung gần giống vẫn
    trùng được, vì feature có thêm phần text thân bài.
    """
    tokens = tokenize(title)
    features = ngram_shingles(tokens)
    extra_source = summary or content or ""
    if extra_source:
        extra_tokens = tokenize(extra_source[:300])
        features.extend(ngram_shingles(extra_tokens))
    if not features:
        features = [normalize_title(title)] or []
    return [feature for feature in features if feature]


# --------------------------------------------------------------------------
# Thời gian
# --------------------------------------------------------------------------

_ISO_CLEAN_RE = re.compile(r"(\.\d{1,6})\d*")


def parse_datetime(value: object) -> datetime | None:
    """Parse mọi kiểu mốc thời gian thường gặp trên website về UTC.

    Hỗ trợ: datetime, ISO-8601, RFC-2822 (RSS), dạng "15 phút trước" không hỗ trợ
    (trả None), epoch giây/mili-giây.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # mili-giây
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=UTC)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if text.isdigit() and len(text) in (10, 13):
        return parse_datetime(int(text))

    candidate = _ISO_CLEAN_RE.sub(lambda m: m.group(1), text)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    candidate = candidate.replace(" ", "T", 1) if "T" not in candidate and " " in candidate else candidate
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed is None:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def to_iso(value: datetime | None) -> str | None:
    """Chuyển datetime về chuỗi ISO-8601 UTC để lưu SQLite."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def utcnow() -> datetime:
    """Thời điểm hiện tại theo UTC (tz-aware)."""
    return datetime.now(UTC)


def hours_since(value: datetime | None, now: datetime | None = None) -> float:
    """Số giờ đã trôi qua kể từ ``value``. Nếu không có mốc thời gian trả +inf."""
    if value is None:
        return math.inf
    reference = now or utcnow()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0.0, (reference - value).total_seconds() / 3600.0)
