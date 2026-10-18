from bot.tunnel import parse_url


def test_parse_url():
    line = "2026-10-16T10:12:01Z INF |  https://quiet-river-bold-moon.trycloudflare.com                          |"
    assert parse_url(line) == "https://quiet-river-bold-moon.trycloudflare.com"
    assert parse_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None


def test_tunnel_errors_logged_rarely(caplog):
    from bot.tunnel import Tunnel
    t = Tunnel("cloudflared", 8080, lambda url: None)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            t._log_error("2026 ERR Connection terminated")
    assert len(caplog.records) == 1
