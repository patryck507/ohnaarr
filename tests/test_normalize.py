from ohnaarr import normalize


def test_repairs_single_layer_mojibake():
    assert normalize.repair("MÃ¤dchen") == "Mädchen"
    assert normalize.repair("KÃ¶ln 50667") == "Köln 50667"
    assert normalize.repair("GroÃŸstadtrevier") == "Großstadtrevier"


def test_repairs_double_layer_mojibake():
    assert normalize.repair("MÃÂ¤dchen") == "Mädchen"
    assert normalize.repair("MÃÂ¤dchen") == "Mädchen"


def test_repair_is_idempotent_on_clean_text():
    for text in ("Mädchen", "Tatort", "Die Pfefferkörner", "Alarm für Cobra 11"):
        assert normalize.repair(text) == text
        assert normalize.repair(normalize.repair(text)) == text


def test_repairs_entities_and_percent_escapes():
    assert normalize.repair("M&auml;dchen") == "Mädchen"
    assert normalize.repair("M&#228;dchen") == "Mädchen"
    assert normalize.repair("M%C3%A4dchen") == "Mädchen"


def test_composes_decomposed_umlaut():
    assert normalize.repair("Mädchen") == "Mädchen"


def test_fold_ae_transliterates():
    assert normalize.fold_ae("Mädchen") == "maedchen"
    assert normalize.fold_ae("Großstadtrevier") == "grossstadtrevier"
    assert normalize.fold_ae("Die Pfefferkörner") == "die pfefferkoerner"


def test_fold_plain_drops_diacritics():
    assert normalize.fold_plain("Mädchen") == "madchen"
    assert normalize.fold_plain("Köln 50667") == "koln 50667"


def test_folds_converge_for_all_spellings():
    variants = ["Mädchen", "MÃ¤dchen", "Maedchen", "Madchen", "maedchen.wg"]
    keys = [set(normalize.similarity_candidates(v)) for v in variants]
    # every variant shares a key with the canonical spelling
    canonical = keys[0]
    for other in keys[1:]:
        assert canonical & other or any(k.startswith(c) for k in other for c in canonical)


def test_clean_release_title_strips_noise():
    cleaned = normalize.clean_release_title(
        "Die.Pfefferkoerner.S19E04.German.1080p.WEB.h264-GROUP"
    )
    assert "1080p" not in cleaned.lower()
    assert "german" not in cleaned.lower()
    assert "web" not in cleaned.lower()
    assert "Pfefferkoerner" in cleaned


def test_normalised_filename_keeps_structure():
    out = normalize.normalised_filename("Mädchen.WG.S01E02.Frühstück.German.1080p.mkv")
    assert "Maedchen" in out
    assert "S01E02" in out
    assert "Fruehstueck" in out


def test_apply_replacements_is_case_insensitive():
    assert normalize.apply_replacements("staffel 1", [("Staffel", "Season")]) == "Season 1"
