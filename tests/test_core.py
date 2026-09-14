
from pathlib import Path
from speechmatics_test.evaluation import evaluate
from speechmatics_test.medical_layer import MedicalLayer
from speechmatics_test.text import normalize_text

ROOT=Path(__file__).resolve().parents[1]

def test_text_normalization():
    assert normalize_text("كیست   هموراژیک") == "کیست هموراژیک"

def test_number_accuracy():
    x=evaluate("دوز 20 میلی گرم", "دوز 20 میلی گرم")
    assert x["number_accuracy"] == 1.0

def test_alias_canonicalization():
    layer=MedicalLayer(ROOT)
    out,hits=layer.normalize("کیست هموراژیک در تخمدان")
    assert "cyst" in out
    assert hits
