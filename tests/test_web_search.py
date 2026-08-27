from mochi.skills.web_search.handler import (
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
