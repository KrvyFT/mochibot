from mochi.skills.web_search.handler import (
    _bing_request_options,
    _extract_bing_results,
    _uses_english_search,
)


def test_bing_search_parser_extracts_only_organic_results():
    html = b"""
        <li class="b_algo">
          <h2><a href="https://example.com/one">First <strong>result</strong></a></h2>
          <div class="b_caption"><p>A useful <em>snippet</em>.</p></div>
        </li>
        <li class="b_ad">
          <h2><a href="https://ads.example/">Advertisement</a></h2>
          <p>Not an organic result.</p>
        </li>
        <li class="b_algo extra-class">
          <h2><a href="https://example.com/two">Second result</a></h2>
          <p>Another snippet.</p>
        </li>
    """

    assert _extract_bing_results(html, "utf-8", 5) == (
        "1. First result\n"
        "   https://example.com/one\n"
        "   A useful snippet.\n\n"
        "2. Second result\n"
        "   https://example.com/two\n"
        "   Another snippet."
    )


def test_search_language_follows_query_content():
    assert _uses_english_search("word of the day Merriam-Webster")
    assert not _uses_english_search("2026年8月27日 今日新闻")
    assert not _uses_english_search("Merriam-Webster 今日单词")
    assert not _uses_english_search("Python 𠀀")
    assert not _uses_english_search("Python カタカナ")
    assert not _uses_english_search("Python 한국어")

    english_headers, english_params, english_cookies = _bing_request_options(
        "word of the day Merriam-Webster",
        5,
    )
    assert english_headers["Accept-Language"] == "en-US,en;q=0.9"
    assert english_params["ensearch"] == "1"
    assert english_cookies == {"SRCHHPGUSR": "SRCHLANG=EN"}

    chinese_headers, chinese_params, chinese_cookies = _bing_request_options(
        "2026年8月27日 今日新闻",
        5,
    )
    assert chinese_headers["Accept-Language"] == "zh-CN,zh;q=0.9"
    assert "ensearch" not in chinese_params
    assert not chinese_cookies
