import bot.setup_env as s


def test_proxy_line_from_provider_is_converted_and_env_updated(tmp_path, monkeypatch):
    assert s.proxy_url('p1.example.net:12566:user:pass') == 'http://user:pass@p1.example.net:12566'
    assert s.proxy_url('http://u:p@h:1') == 'http://u:p@h:1'
    text = 'A=1\nBOT_TOKEN=\nB=2\n'
    assert s.set_value(text, 'BOT_TOKEN', '1:x') == 'A=1\nBOT_TOKEN=1:x\nB=2\n'
    assert s.set_value(text, 'NEW', 'v').endswith('NEW=v\n')


def test_config_accepts_provider_proxy_format():
    from bot.config import proxy_url
    assert proxy_url("p1.example.net:12566:user:pass") == "http://user:pass@p1.example.net:12566"
    assert proxy_url("socks5://h:1") == "socks5://h:1" and proxy_url("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert proxy_url("") == ""
