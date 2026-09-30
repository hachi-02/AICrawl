"""Test cho các hàm chuẩn hóa: URL, tiêu đề, thời gian, simhash."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.dedupe.normalizer import (
    canonicalize_url,
    content_hash,
    extract_host,
    hamming_distance,
    hours_since,
    jaccard,
    ngram_shingles,
    normalize_title,
    overlap_coefficient,
    parse_datetime,
    simhash,
    simint,
    simhex,
    token_set,
    tokenize,
)


class TestCanonicalizeUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://www.coindesk.com/markets/2026/09/29/btc/", "coindesk.com/markets/2026/09/29/btc"),
            ("http://coindesk.com/markets/2026/09/29/btc", "coindesk.com/markets/2026/09/29/btc"),
            ("https://coindesk.com/markets/2026/09/29/btc?utm_source=x&utm_medium=y", "coindesk.com/markets/2026/09/29/btc"),
            ("https://coindesk.com/markets/2026/09/29/btc?fbclid=abc", "coindesk.com/markets/2026/09/29/btc"),
            ("https://coindesk.com/markets//2026///09/29/btc", "coindesk.com/markets/2026/09/29/btc"),
            ("https://coindesk.com/markets/2026/09/29/btc#section", "coindesk.com/markets/2026/09/29/btc"),
            ("https://coindesk.com:443/markets/2026/09/29/btc/", "coindesk.com/markets/2026/09/29/btc"),
            ("coindesk.com/markets/2026/09/29/btc", "coindesk.com/markets/2026/09/29/btc"),
        ],
    )
    def test_canonical_form(self, raw: str, expected: str) -> None:
        assert canonicalize_url(raw) == f"https://{expected}"

    def test_keeps_meaningful_query(self) -> None:
        url = "https://example.com/news/btc?currency=usd&utm_source=twitter"
        assert canonicalize_url(url) == "https://example.com/news/btc?currency=usd"

    def test_amp_suffix_removed(self) -> None:
        assert canonicalize_url("https://www.coindesk.com/news/btc/amp/") == "https://coindesk.com/news/btc"

    def test_empty(self) -> None:
        assert canonicalize_url("") == ""

    def test_extract_host(self) -> None:
        assert extract_host("https://www.theverge.com/x") == "theverge.com"


class TestNormalizeTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("OpenAI releases GPT-5 - TechCrunch", "openai releases gpt 5"),
            ("Bitcoin ETF approved! | CoinDesk", "bitcoin etf approved"),
            ("  Multiple   spaces   here  ", "multiple spaces here"),
            ("Caf&eacute; &amp; Co. &quot;quoted&quot;", "café co quoted"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert normalize_title(raw) == expected

    def test_tokenize_drops_stopwords(self) -> None:
        assert tokenize("The new model of OpenAI is here") == ["model", "openai"]

    def test_tokenize_keeps_numeric_tokens(self) -> None:
        assert tokenize("OpenAI releases GPT-5 and LLaMA 3") == ["openai", "releases", "gpt", "5", "llama", "3"]

    def test_token_set(self) -> None:
        assert token_set("OpenAI GPT-5") == frozenset({"openai", "gpt", "5"})

    def test_jaccard(self) -> None:
        assert jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0
        assert jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == pytest.approx(1 / 3)
        assert jaccard(frozenset(), frozenset({"a"})) == 0.0

    def test_overlap_coefficient(self) -> None:
        # tập nhỏ nằm trọn trong tập lớn -> 1.0, khác với Jaccard
        assert overlap_coefficient(frozenset({"a", "b"}), frozenset({"a", "b", "c", "d"})) == 1.0
        assert overlap_coefficient(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0
        assert overlap_coefficient(frozenset({"a", "b"}), frozenset({"b", "c", "d", "e"})) == 0.5
        assert overlap_coefficient(frozenset(), frozenset({"a"})) == 0.0

    def test_suffix_stripped_only_when_head_is_long_enough(self) -> None:
        assert normalize_title("OpenAI releases GPT-5 model - TechCrunch") == "openai releases gpt 5 model"
        # tiêu đề ngắn: " - Co. ... " là tên công ty, không phải hậu tố nguồn
        assert normalize_title("Caf&eacute; &amp; Co. &quot;quoted&quot;") == "café co quoted"


class TestSimhash:
    def test_shingles(self) -> None:
        assert ngram_shingles(["a", "b", "c", "d"]) == ["a b c", "b c d"]
        assert ngram_shingles(["a", "b"]) == ["a b"]
        assert ngram_shingles([]) == []

    def test_identical_text_distance_zero(self) -> None:
        features = ngram_shingles(tokenize("OpenAI releases GPT-5 today"))
        assert hamming_distance(simhash(features), simhash(list(features))) == 0

    def test_similar_titles_are_close(self) -> None:
        base = ngram_shingles(tokenize("Bitcoin spot ETF approved by SEC after years"))
        near = ngram_shingles(tokenize("Bitcoin spot ETF approved by the SEC today"))
        # simhash trên tiêu đề rất ngắn không ổn định -> chỉ dùng làm tín hiệu
        # phụ để phân tích, quyết định khớp dựa trên tập token.
        assert hamming_distance(simhash(base), simhash(near)) < 32

    def test_different_titles_are_far(self) -> None:
        base = ngram_shingles(tokenize("Bitcoin spot ETF approved by SEC after years"))
        other = ngram_shingles(tokenize("Ethereum network outage resolved by developers"))
        assert hamming_distance(simhash(base), simhash(other)) > 8

    def test_hex_roundtrip(self) -> None:
        value = simhash(ngram_shingles(tokenize("Hello world example")))
        assert simint(simhex(value)) == value
        assert simint("") == 0
        assert simint("zzz") == 0


class TestContentHash:
    def test_same_title_same_source_equal(self) -> None:
        a = content_hash("OpenAI releases GPT-5", "TechCrunch")
        b = content_hash("openai   releases gpt 5!", "techcrunch")
        assert a == b

    def test_different_source_different_hash(self) -> None:
        assert content_hash("Same headline", "A") != content_hash("Same headline", "B")

    def test_is_sha256_hex(self) -> None:
        assert len(content_hash("x", "y")) == 64


class TestParseDatetime:
    @pytest.mark.parametrize(
        "raw",
        [
            "2026-09-29T10:00:00Z",
            "2026-09-29T10:00:00+00:00",
            "Tue, 29 Sep 2026 10:00:00 GMT",
            "Tue, 29 Sep 2026 10:00:00 +0000",
            1788074400,
            "1788074400",
        ],
    )
    def test_supported_formats(self, raw: object) -> None:
        parsed = parse_datetime(raw)
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.astimezone(UTC).year == 2026

    def test_milliseconds_epoch(self) -> None:
        assert parse_datetime(1788074400000) == parse_datetime(1788074400)

    @pytest.mark.parametrize("raw", [None, "", "   ", "not a date", "yesterday"])
    def test_invalid(self, raw: object) -> None:
        assert parse_datetime(raw) is None

    def test_datetime_passthrough(self) -> None:
        naive = datetime(2026, 9, 29, 10, 0)
        assert parse_datetime(naive) == naive.replace(tzinfo=UTC)

    def test_hours_since(self) -> None:
        now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
        assert hours_since(now - timedelta(hours=3), now) == 3.0
        assert hours_since(None, now) == float("inf")
        assert hours_since(now + timedelta(hours=5), now) == 0.0
