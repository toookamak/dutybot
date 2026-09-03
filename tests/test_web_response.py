from dutybot.web import html_response


def test_html_response_splits_charset():
    resp = html_response("<p>ok</p>")
    assert resp.content_type == "text/html"
    assert resp.charset == "utf-8"
    assert resp.status == 200


def test_html_response_unauthorized():
    resp = html_response("no", status=401)
    assert resp.status == 401
    assert resp.content_type == "text/html"
