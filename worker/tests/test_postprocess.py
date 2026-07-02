from worker.postprocess import html_img_to_markdown, html_table_to_markdown, process_markdown


def test_html_table_to_markdown() -> None:
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    md = html_table_to_markdown(html)
    assert "| A | B |" in md
    assert "| --- | --- |" in md
    assert "| 1 | 2 |" in md


def test_html_table_colspan_pads_cells() -> None:
    html = '<table><tr><td colspan="2">wide</td></tr><tr><td>a</td><td>b</td></tr></table>'
    md = html_table_to_markdown(html)
    # colspan yields a 2-column table (empty trailing cell, not duplicated content)
    assert "| --- | --- |" in md
    assert "wide" in md
    assert "| a | b |" in md


def test_html_img_generic_alt_is_stripped() -> None:
    md = html_img_to_markdown('<img alt="Image" src="pic.png"/>', None)
    assert md.strip() == "![](pic.png)"


def test_process_markdown_converts_table_and_fixes_alt() -> None:
    src = '<table><tr><td>x</td></tr></table>\n<img alt="Image" src="a.png"/>'
    md = process_markdown(src, None, "en", images=True)
    assert "| x |" in md
    assert "![](a.png)" in md
