from html.parser import HTMLParser


class SectionActionParser(HTMLParser):
    """Collect rendered enrollment controls grouped by their section row."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = {}
        self._row = None
        self._form = None
        self._button = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "li" and "data-section-id" in attrs:
            self._row = {
                "id": attrs["data-section-id"],
                "forms": [],
                "buttons": [],
            }
        elif tag == "form" and self._row is not None:
            self._form = {"action": attrs.get("action"), "buttons": []}
            self._row["forms"].append(self._form)
        elif tag == "button" and self._row is not None:
            self._button = {
                "disabled": "disabled" in attrs,
                "type": attrs.get("type", "submit"),
                "text": [],
            }

    def handle_data(self, data):
        if self._button is not None:
            self._button["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "button" and self._button is not None:
            button = {
                "disabled": self._button["disabled"],
                "type": self._button["type"],
                "text": "".join(self._button["text"]).strip(),
            }
            if self._form is None:
                self._row["buttons"].append(button)
            else:
                self._form["buttons"].append(button)
            self._button = None
        elif tag == "form":
            self._form = None
        elif tag == "li" and self._row is not None:
            self.rows[self._row["id"]] = self._row
            self._row = None


def section_actions(html):
    parser = SectionActionParser()
    parser.feed(html)
    return parser.rows
