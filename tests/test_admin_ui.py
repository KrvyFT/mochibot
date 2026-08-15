from html.parser import HTMLParser
from pathlib import Path


class _ElementAttributeParser(HTMLParser):
    def __init__(self, element_id: str):
        super().__init__()
        self.element_id = element_id
        self.attributes: dict[str, str | None] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if attributes.get("id") == self.element_id:
            self.attributes = attributes


def test_model_modal_does_not_close_when_backdrop_is_clicked():
    index_html = (
        Path(__file__).parents[1] / "mochi" / "admin" / "index.html"
    ).read_text(encoding="utf-8")
    parser = _ElementAttributeParser("model-modal")
    parser.feed(index_html)

    assert parser.attributes is not None
    assert "onclick" not in parser.attributes
