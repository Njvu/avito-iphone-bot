from bot.models import canonical_model, detect_phone, reject_reason


def test_avito_titles():
    assert detect_phone("iPhone 14 Pro, 256 ГБ, SIM + eSIM") == ("14 Pro", 256) or detect_phone("iPhone 14 Pro, 256 ГБ, SIM + eSIM").model == "14 Pro"
    assert detect_phone("iPhone 13 mini, 128 ГБ").model == "13 mini"
    assert detect_phone("iPhone 16e, 128 ГБ").model == "16e"
    assert detect_phone("iPhone 17 Pro Max, 2 ТБ").storage_gb == 2048
    assert detect_phone("iPhone Air, 256 ГБ").model == "Air"
    assert detect_phone("iPhone 17 Air 512GB").model == "Air"


def test_strict_variants_and_price_not_model():
    assert detect_phone("iPhone 13 128 ГБ", "Цена 17 000, торг").model == "13"
    assert detect_phone("Айфон 12 про макс 256", "").model == "12 Pro Max"
    assert detect_phone("Айфон 13 мини 128гб").model == "13 mini"
    assert detect_phone("iPhone 12 Pro Max 256 гб").model == "12 Pro Max"
    assert detect_phone("iPhone 14 Pro") == ("14 Pro", 0) or detect_phone("iPhone 14 Pro").storage_gb == 0
    assert detect_phone("Чехол для iPhone 15 128 ГБ") is None


def test_homoglyphs():
    assert detect_phone("iPhоne 14 Рrо, 256 ГБ").model == "14 Pro"


def test_reject():
    assert reject_reason("iPhone 15, 128 ГБ, eSIM") == "только eSIM"
    assert reject_reason("iPhone 15, 128 ГБ, SIM + eSIM") is None
    assert reject_reason("iPhone 13, 128 ГБ", "R-SIM, лоченый") == "лочка/R-SIM"
    assert reject_reason("iPhone 13, 128 ГБ", "Восстановленный") == "восстановленный/обменка"


def test_canonical():
    assert canonical_model("iphone 13 pro max") == "13 Pro Max"
    assert canonical_model("air") == "Air"
    assert canonical_model("13 Про") is None


def test_old_models():
    assert detect_phone("iPhone Xs Max, 256 ГБ").model == "XS Max"
    assert detect_phone("iPhone XR, 64 ГБ").model == "XR"
    assert detect_phone("iPhone X, 64 ГБ").model == "X"
    assert detect_phone("Айфон 10 64гб").model == "X"
    assert detect_phone("iPhone 11 Pro Max, 256 ГБ").model == "11 Pro Max"
    assert detect_phone("iPhone 110, 64 ГБ") is None
    assert canonical_model("xs max") == "XS Max"
