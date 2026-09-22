"""Versioned benchmark dataset for the nursing normalization pipeline.

Design rules (the "benchmark contamination" section of the spec):

* Every case carries the SPOKEN form and the EXPECTED normalized form.
* The expected form is never shorter in clinical content than the spoken
  form: no clinically meaningful token may be dropped.
* Expected values are derived from the deterministic rules the code
  actually implements, not from wishful "an ideal system would say X".
* ASR errors are NOT in scope here. These fixtures start from a transcript
  that the ASR already produced; the benchmark measures what the
  POST-PROCESSOR does with it. A case may therefore never claim that the
  pipeline repaired a mis-recognition.
* Each case declares its ``stage`` so the report can separate
  generic normalization / medical canonicalization / number-time-unit
  normalization / grammar-format normalization instead of lumping them
  into one score.

``DATASET_VERSION`` must be bumped whenever a fixture changes, so two
result files are never compared as if they measured the same data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DATASET_VERSION = "nursing-2026.09.22.4"

#: Stage a case exercises. Reported separately (never averaged together).
STAGES = (
    "generic_normalization",     # script/digits/whitespace (text.normalize_text)
    "medical_canonicalization",  # Aho-Corasick lexical layer
    "number_time_unit",          # deterministic numeric/clock/unit rules
    "grammar_format",            # repetition/punctuation/typography cleanup
    "paragraph",                 # full realistic nursing documentation
)


@dataclass(frozen=True)
class Case:
    """One benchmark fixture.

    ``spoken``    - the transcript as it arrives from the ASR.
    ``expected``  - the expected post-processed output.
    ``stage``     - which pipeline stage this case measures.
    ``group``     - terminology group, for the per-group accuracy table.
    ``expected_terms``    - canonical medical terms that must appear.
    ``expected_numbers``  - numeric tokens that must survive verbatim.
    ``expect_warning``    - the case must raise a polish warning (ambiguity
                            that the system must surface rather than guess).
    ``note``      - why this fixture exists (audit trail).
    """

    id: str
    spoken: str
    expected: str
    stage: str
    group: str
    expected_terms: tuple[str, ...] = ()
    expected_numbers: tuple[str, ...] = ()
    expect_warning: bool = False
    note: str = ""


# ------------------------------------------------------- A. matcher groups

TERMINOLOGY_CASES: tuple[Case, ...] = (
    # --- abbreviations
    Case("abbr_ccu", "بیمار در سی سی یو بستری است",
         "بیمار در CCU بستری است",
         "medical_canonicalization", "abbreviations", ("CCU",)),
    Case("abbr_icu", "انتقال به آی سی یو انجام شد",
         "انتقال به ICU انجام شد",
         "medical_canonicalization", "abbreviations", ("ICU",)),
    Case("abbr_npo", "بیمار ان پی او است",
         "بیمار NPO است",
         "medical_canonicalization", "abbreviations", ("NPO",)),
    Case("abbr_gcs", "جی سی اس بیمار ثبت شد",
         "GCS بیمار ثبت شد",
         "medical_canonicalization", "abbreviations", ("GCS",)),

    # --- vital signs
    Case("vital_bp", "بی پی بیمار کنترل شد",
         "BP بیمار کنترل شد",
         "medical_canonicalization", "vital_signs", ("BP",)),
    Case("vital_hr_fa", "اچ آر بیمار ثبت گردید",
         "HR بیمار ثبت گردید",
         "medical_canonicalization", "vital_signs", ("HR",)),
    Case("vital_rr_fa", "آر آر بیمار شمارش شد",
         "RR بیمار شمارش شد",
         "medical_canonicalization", "vital_signs", ("RR",)),
    Case("vital_spo2_fa", "اس پی او دو بیمار پایش می شود",
         "SpO2 بیمار پایش می\u200cشود",
         "medical_canonicalization", "vital_signs", ("SpO2",)),
    Case("vital_temp_fa", "تمپ بیمار اندازه گیری شد",
         "Temp بیمار اندازه گیری شد",
         "medical_canonicalization", "vital_signs", ("Temp",)),

    # --- laboratory
    Case("lab_cbc", "سی بی سی درخواست شد",
         "CBC درخواست شد",
         "medical_canonicalization", "laboratory", ("CBC",)),
    Case("lab_fbs", "اف بی اس بیمار چک شد",
         "FBS بیمار چک شد",
         "medical_canonicalization", "laboratory", ("FBS",)),
    Case("lab_inr", "آی ان آر بیمار گزارش شد",
         "INR بیمار گزارش شد",
         "medical_canonicalization", "laboratory", ("INR",)),
    Case("lab_ua", "آزمایش ادرار ارسال شد",
         "U/A ارسال شد",
         "medical_canonicalization", "laboratory", ("U/A",)),

    # --- medication / routes / devices
    Case("route_iv_fa", "دارو به صورت آی وی تزریق شد",
         "دارو به صورت IV تزریق شد",
         "medical_canonicalization", "routes", ("IV",)),
    Case("route_im_fa", "تزریق آی ام انجام شد",
         "تزریق IM انجام شد",
         "medical_canonicalization", "routes", ("IM",)),
    Case("device_iv_line", "لاین وریدی برقرار است",
         "IV line برقرار است",
         "medical_canonicalization", "devices", ("IV line",)),
    Case("device_foley", "سوند فولی تعبیه شد",
         "Foley catheter تعبیه شد",
         "medical_canonicalization", "devices", ("Foley catheter",)),

    # --- procedures / imaging
    Case("proc_ct", "در سی تی اسکن ضایعه دیده شد",
         "در CT scan ضایعه دیده شد",
         "medical_canonicalization", "procedures", ("CT scan",)),
    Case("proc_mri", "ام آر آی درخواست شد",
         "MRI درخواست شد",
         "medical_canonicalization", "procedures", ("MRI",)),

    # --- assessments / scales
    Case("assess_morse", "مقیاس مورس تکمیل شد",
         "Morse Fall Scale تکمیل شد",
         "medical_canonicalization", "assessments", ("Morse Fall Scale",)),
    Case("assess_braden", "مقیاس برادن ثبت شد",
         "Braden Scale ثبت شد",
         "medical_canonicalization", "assessments", ("Braden Scale",)),
    Case("assess_nursing", "ارزیابی پرستاری انجام شد",
         "nursing assessment انجام شد",
         "medical_canonicalization", "assessments", ("nursing assessment",)),

    # --- fall risk / pressure injury
    Case("fall_precautions", "احتیاطات سقوط رعایت شد",
         "fall precautions رعایت شد",
         "medical_canonicalization", "fall_risk", ("fall precautions",)),
    Case("pressure_injury", "زخم بستر مشاهده نشد",
         "pressure injury مشاهده نشد",
         "medical_canonicalization", "pressure_injury", ("pressure injury",)),

    # --- IV complications
    Case("iv_phlebitis", "فلبیت در محل کاتتر دیده نشد",
         "phlebitis در محل کاتتر دیده نشد",
         "medical_canonicalization", "iv_terminology", ("phlebitis",)),
    Case("iv_infiltration", "نشت دارویی گزارش نشد",
         "infiltration گزارش نشد",
         "medical_canonicalization", "iv_terminology", ("infiltration",)),

    # --- cardiovascular
    Case("cv_chest_pain", "بیمار چست پین دارد",
         "بیمار chest pain دارد",
         "medical_canonicalization", "cardiovascular", ("chest pain",)),
    Case("cv_dyspnea", "تنگی نفس گزارش شد",
         "shortness of breath گزارش شد",
         "medical_canonicalization", "cardiovascular",
         ("shortness of breath",)),

    # --- orthopedic
    Case("ortho_fracture", "شکستگی استخوان ران تایید شد",
         "femur fracture تایید شد",
         "medical_canonicalization", "orthopedic", ("femur fracture",)),
    Case("ortho_traction", "کشش پوستی برقرار است",
         "skin traction برقرار است",
         "medical_canonicalization", "orthopedic", ("skin traction",)),

    # --- skin assessment
    Case("skin_ecchymosis", "اکیموز در ساعد چپ مشاهده شد",
         "Ecchymosis در ساعد چپ مشاهده شد",
         "medical_canonicalization", "skin_assessment", ("Ecchymosis",)),
    Case("skin_turgor", "تورگور پوست طبیعی است",
         "skin turgor طبیعی است",
         "medical_canonicalization", "skin_assessment", ("skin turgor",)),

    # --- Persianized English pronunciations
    Case("persianized_saturation", "ساتوریشن بیمار پایش شد",
         "oxygen saturation بیمار پایش شد",
         "medical_canonicalization", "persianized_english",
         ("oxygen saturation",)),
    Case("persianized_nurse_call", "نرس کال در دسترس بیمار است",
         "Nurse call در دسترس بیمار است",
         "medical_canonicalization", "persianized_english", ("Nurse call",)),
)


# ------------------------------------------------- B. matcher safety cases

#: A lexical matcher must never rewrite ordinary prose into clinical
#: shorthand. Each case asserts the text is returned UNCHANGED.
SAFETY_CASES: tuple[Case, ...] = (
    Case("safe_now", "Please do it now and document the result",
         "Please do it now and document the result",
         "medical_canonicalization", "boundary_safety",
         note="lowercase 'now' is the English adverb, not the STAT shorthand"),
    Case("safe_or", "Give the tablet or the syrup to the patient",
         "Give the tablet or the syrup to the patient",
         "medical_canonicalization", "boundary_safety",
         note="lowercase 'or' is a conjunction, not the operating room"),
    Case("safe_substring_action", "The care plan requires immediate action",
         "The care plan requires immediate action",
         "medical_canonicalization", "boundary_safety",
         note="'action' contains 'act'/'ct' - substring matching must not fire"),
    Case("safe_substring_delivery", "delivery of the medication was delayed",
         "delivery of the medication was delayed",
         "medical_canonicalization", "boundary_safety",
         note="boundary safety inside a longer token"),
    Case("safe_negation", "بیمار cardiovascular disease ندارد",
         "بیمار cardiovascular disease ندارد",
         "medical_canonicalization", "semantics",
         ("cardiovascular disease",),
         note="negation must be preserved verbatim; no semantic inference"),
    Case("safe_persian_one", "یک بیمار در بخش پذیرش شد",
         "یک بیمار در بخش پذیرش شد",
         "number_time_unit", "number_safety",
         note="'یک' as the article 'a' must not become the digit 1"),
    Case("safe_ct_form", "سی تی اسکن قفسه سینه انجام شد",
         "CT scan قفسه سینه انجام شد",
         "medical_canonicalization", "number_safety", ("CT scan",),
         note="'سی' inside 'سی تی' must not be read as the number 30"),
)


# ----------------------------------------------------- C. number accuracy

NUMBER_CASES: tuple[Case, ...] = (
    Case("num_integer", "دوز 500 میلی گرم تجویز شد",
         "دوز 500 mg تجویز شد",
         "number_time_unit", "numbers_integer", ("mg",), ("500",)),
    Case("num_decimal", "دمای بدن 36.7 درجه سانتی گراد بود",
         "Temp: 36.7 °C بود",
         "number_time_unit", "numbers_decimal", ("Temp", "°C"), ("36.7",)),
    Case("num_persian_digits", "فشار خون ۱۴۰/۸۵ میلی متر جیوه بود",
         "BP: 140/85 mmHg بود",
         "number_time_unit", "numbers_persian_digits", ("BP", "mmHg"), ("140/85",)),
    Case("num_arabic_indic", "علائم حیاتی ٨٨ و ٩٧ ثبت شد",
         "vital signs 88 و 97 ثبت شد",
         "generic_normalization", "numbers_arabic_indic",
         ("vital signs",), ("88", "97"),
         note="Arabic-Indic digits fold to ASCII; the fixture avoids 'نبض', "
              "which is a legitimate PR alias, so this case measures the "
              "digit folding only"),
    Case("num_ratio", "blood pressure 120/80 mmHg",
         "BP: 120/80 mmHg",
         "number_time_unit", "numbers_ratio", ("mmHg",), ("120/80",)),
    Case("num_ratio_spoken_fa", "فشار خون صد و چهل روی هشتاد و پنج",
         "BP: 140/85",
         "number_time_unit", "numbers_ratio", ("BP",), ("140/85",),
         note="a dictated BP: the two spoken cardinals plus the separator "
              "'روی' are the charted ratio 140/85. This stayed as "
              "'140 روی 85' until the spoken-ratio rule was added"),
    Case("num_ratio_spoken_en", "blood pressure 140 over 85 mmHg",
         "BP: 140/85 mmHg",
         "number_time_unit", "numbers_ratio", ("mmHg",), ("140/85",)),
    Case("num_ratio_word_not_a_ratio", "پانسمان روی زخم تعویض شد",
         "پانسمان روی زخم تعویض شد",
         "number_time_unit", "boundary_safety", (), (),
         note="'روی' is an ordinary preposition ('on'); without numerals on "
              "both sides it must never become a slash"),
    Case("num_malformed_cardinal", "درجه حرارت سی و شش و هفت درجه سانتی گراد",
         "درجه حرارت سی و شش و هفت °C",
         "number_time_unit", "numbers_decimal", ("°C",), (),
         expect_warning=True,
         note="SAFETY: this is how 36.7 is dictated. Summing the parts "
              "produced 43 - a temperature nobody said. An unparseable "
              "cardinal must survive verbatim and raise a warning"),
    Case("num_percent", "oxygen saturation 97 درصد",
         "SpO2: 97%",
         "number_time_unit", "numbers_percent", ("%",), ("97",)),
    Case("num_score", "نمره درد 4 از 10 گزارش شد",
         "pain score: 4 از 10 گزارش شد",
         "number_time_unit", "numbers_score", ("pain score",), ("4", "10"),
         note="'pain score' is a charted label, so the label rule gives it "
              "the standard 'LABEL: value' punctuation"),
    Case("num_dose_ml", "500 میلی لیتر سرم انفوزیون شد",
         "500 mL سرم infusion شد",
         "number_time_unit", "numbers_dose", ("mL", "infusion"), ("500",)),
    Case("num_age_spoken", "مددجو آقای سی و پنج ساله",
         "مددجو آقای 35 ساله",
         "number_time_unit", "numbers_spoken", (), ("35",)),
    Case("num_bpm", "ضربان 88 بار در دقیقه",
         "ضربان 88 بار در دقیقه",
         "number_time_unit", "numbers_integer", (), ("88",)),
)


# ------------------------------------------------- D. time normalization

TIME_CASES: tuple[Case, ...] = (
    Case("time_half", "ده و نیم", "10:30",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_minutes_word", "ده و سی دقیقه", "10:30",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_with_lead", "ساعت ده و سی دقیقه", "ساعت 10:30",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_digits_word", "10 و 30 دقیقه", "10:30",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_persian_digits", "۱۰ و ۳۰ دقیقه", "10:30",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_colon", "10:30", "10:30",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_bare_pair", "ساعت ده سی", "ساعت 10:30",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_morning", "ده و نیم صبح", "10:30 صبح",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_night", "ده و نیم شب", "10:30 شب",
         "number_time_unit", "time", (), ("10:30",)),
    Case("time_quarter", "ده و ربع", "10:15",
         "number_time_unit", "time", (), ("10:15",)),
    Case("time_unbound_number", "10 و 30 دقیقه 90", "10:30 90",
         "number_time_unit", "time", (), ("10:30", "90"),
         expect_warning=True,
         note="the trailing 90 must survive AND be reported, never absorbed"),
)


# ------------------------------------------------- E. unit normalization

UNIT_CASES: tuple[Case, ...] = (
    Case("unit_mmhg_fa", "فشار 140/85 میلی متر جیوه",
         "فشار 140/85 mmHg",
         "number_time_unit", "units", ("mmHg",), ("140/85",)),
    Case("unit_percent_fa", "اشباع اکسیژن 97 درصد",
         "SpO2: 97%",
         "number_time_unit", "units", ("SpO2", "%"), ("97",),
         note="'اشباع' on its own is the ordinary word 'saturation' and is "
              "deliberately NOT an SpO2 alias; the full phrase is"),
    Case("unit_celsius_fa", "36.7 درجه سانتی گراد",
         "36.7 °C",
         "number_time_unit", "units", ("°C",), ("36.7",)),
    Case("unit_mg_fa", "20 میلی گرم", "20 mg",
         "number_time_unit", "units", ("mg",), ("20",)),
    Case("unit_mcg_fa", "50 میکروگرم", "50 mcg",
         "number_time_unit", "units", ("mcg",), ("50",)),
    Case("unit_ml_fa", "250 میلی لیتر", "250 mL",
         "number_time_unit", "units", ("mL",), ("250",)),
    Case("unit_l_fa", "2 لیتر", "2 L",
         "number_time_unit", "units", ("L",), ("2",)),
    Case("unit_cm_fa", "3 سانتی متر", "3 cm",
         "number_time_unit", "units", ("cm",), ("3",)),
    Case("unit_attached_en", "36.7°C", "36.7 °C",
         "number_time_unit", "units", (), ("36.7",)),
    Case("unit_separated_en", "97 %", "97%",
         "number_time_unit", "units", (), ("97",)),
    Case("unit_mmhg_en", "140/85 mmhg", "140/85 mmHg",
         "number_time_unit", "units", (), ("140/85",)),
    Case("unit_bpm_en", "88 bpm", "88 bpm",
         "number_time_unit", "units", (), ("88",)),
)


# ---------------------------------------------- F. grammar/format cleanup

GRAMMAR_CASES: tuple[Case, ...] = (
    Case("gram_repeated_word", "نمره نمره درد ثبت شد",
         "pain score ثبت شد",
         "grammar_format", "repetition", ("pain score",),
         note="the stutter must be collapsed BEFORE canonicalization: "
              "otherwise the leftover 'نمره' + 'درد' spells the dictionary "
              "phrase 'نمره درد' and the output keeps a stray 'نمره' in "
              "front of a term the speaker never said twice"),
    Case("gram_repeated_word_plain", "بیمار بیمار در بخش بستری شد",
         "بیمار در بخش بستری شد",
         "grammar_format", "repetition",
         note="pure stutter with no dictionary interaction"),
    Case("gram_repeated_abbrev_safe", "بیمار در سی سی یو بستری است",
         "بیمار در CCU بستری است",
         "grammar_format", "repetition", ("CCU",),
         note="regression: 'سی سی یو' has a REAL repeated syllable and must "
              "survive the duplicated-word cleanup (it became 'سی یو')"),
    Case("gram_echoed_verb", "بیمار هوشیار می باشد. باشد.",
         "بیمار هوشیار می\u200cباشد.",
         "grammar_format", "repetition"),
    Case("gram_echoed_inflection", "درد را ذکر می کند. کنند",
         "درد را ذکر می\u200cکند",
         "grammar_format", "repetition"),
    Case("gram_vital_period", "Temp . 36.7. °C",
         "Temp: 36.7 °C",
         "grammar_format", "punctuation", ("°C",), ("36.7",)),
    Case("gram_vital_bp", "blood pressure. 140/85 mmHg",
         "BP: 140/85 mmHg",
         "grammar_format", "punctuation", ("mmHg",), ("140/85",)),
    Case("gram_vital_spo2", "oxygen saturation و 97%",
         "SpO2: 97%",
         "grammar_format", "punctuation", (), ("97",)),
    Case("gram_vital_hr", "heart rate. 88",
         "HR: 88",
         "grammar_format", "punctuation", (), ("88",)),
    Case("gram_repeated_punct", "بیمار پذیرش شد.. علائم پایدار است",
         "بیمار پذیرش شد. علائم پایدار است",
         "grammar_format", "punctuation"),
    Case("gram_spacing", "بیمار   در   بخش   بستری شد",
         "بیمار در بخش بستری شد",
         "grammar_format", "whitespace"),
    Case("gram_zwnj_prefix", "بیمار درد را ذکر می کند",
         "بیمار درد را ذکر می\u200cکند",
         "grammar_format", "typography"),
)


# --------------------------------------------- G. realistic nursing paragraphs

PARAGRAPH_CASES: tuple[Case, ...] = (
    Case(
        "para_admission",
        "مددجو آقای سی و پنج ساله با شکایت چست پین با تشخیص آنژین ناپایدار "
        "در سرویس دکتر احمدی با پای خود در ساعت ده و سی دقیقه وارد بخش قلب شد",
        "مددجو آقای 35 ساله با شکایت chest pain با تشخیص آنژین ناپایدار "
        "در سرویس دکتر احمدی با پای خود در ساعت 10:30 وارد بخش قلب شد.",
        "paragraph", "admission_assessment",
        ("chest pain",), ("35", "10:30"),
        note="the reference keeps every clinical element of the spoken form",
    ),
    Case(
        "para_vitals",
        "علائم حیاتی شامل blood pressure. 140/85 mmHg و heart rate. 88 و "
        "oxygen saturation و 97% و Temp . 36.7. °C می باشد",
        "vital signs شامل BP: 140/85 mmHg و HR: 88 و "
        "SpO2: 97% و Temp: 36.7 °C می\u200cباشد.",
        "paragraph", "vital_signs",
        ("vital signs", "mmHg", "°C"), ("140/85", "88", "97", "36.7"),
    ),
    Case(
        "para_allergy",
        "مددجو سابقه آلرژی دارویی را ذکر می کند. کنند و سابقه جراحی قبلی ندارد",
        "مددجو سابقه آلرژی دارویی را ذکر می\u200cکند و سابقه جراحی قبلی ندارد.",
        "paragraph", "allergy_history",
        note="negation 'ندارد' must be preserved; stutter tail removed",
    ),
    Case(
        "para_iv_line",
        "لاین وریدی در ساعد چپ برقرار و فیکس می باشد و فلبیت و قرمزی مشاهده نشد",
        "IV line در ساعد چپ برقرار و فیکس می\u200cباشد و phlebitis و "
        "redness مشاهده نشد.",
        "paragraph", "iv_line",
        ("IV line", "phlebitis", "redness"),
    ),
    Case(
        "para_fall",
        "احتیاطات سقوط رعایت شد و نرده های کنار تخت بالا و نرس کال در دسترس "
        "بیمار قرار داده شد",
        "fall precautions رعایت شد و bedside rails بالا و Nurse call در "
        "دسترس بیمار قرار داده شد.",
        "paragraph", "fall_prevention",
        ("fall precautions", "bedside rails", "Nurse call"),
    ),
    Case(
        "para_braden",
        "مقیاس برادن برای مددجو تکمیل و نمره نمره 18 ثبت شد",
        "Braden Scale برای مددجو تکمیل و نمره 18 ثبت شد.",
        "paragraph", "braden_assessment",
        ("Braden Scale",), ("18",),
    ),
    Case(
        "para_morse",
        "مقیاس مورس تکمیل و نمره 45 ثبت گردید و بیمار در معرض خطر سقوط می باشد",
        "Morse Fall Scale تکمیل و نمره 45 ثبت گردید و بیمار در معرض خطر "
        "سقوط می\u200cباشد.",
        "paragraph", "morse_assessment",
        ("Morse Fall Scale",), ("45",),
    ),
    Case(
        "para_education",
        "آموزش های لازم در خصوص استراحت مطلق و رژیم غذایی به مددجو و همراه "
        "داده شد",
        "آموزش\u200cهای لازم در خصوص استراحت مطلق و رژیم غذایی به مددجو و "
        "همراه داده شد.",
        "paragraph", "education",
    ),
    Case(
        "para_physician_notify",
        "پزشک معالج در ساعت ده و نیم اطلاع داده شد و دستورات جدید اخذ گردید",
        "پزشک معالج در ساعت 10:30 اطلاع داده شد و دستورات جدید اخذ گردید.",
        "paragraph", "physician_notification",
        (), ("10:30",),
    ),
    Case(
        "para_consult",
        "مشاوره قلب درخواست شد و بیمار توسط سرویس قلب ویزیت گردید",
        "مشاوره قلب درخواست شد و بیمار توسط سرویس قلب ویزیت گردید.",
        "paragraph", "consultation",
    ),
    Case(
        "para_labs",
        "آزمایشات روتین شامل سی بی سی و آزمایش ادرار و اف بی اس ارسال شد",
        "آزمایشات روتین شامل CBC و U/A و FBS ارسال شد.",
        "paragraph", "routine_labs",
        ("CBC", "U/A", "FBS"),
    ),
    Case(
        "para_skin",
        "تورگور پوست طبیعی و اکیموز در ساعد راست مشاهده شد و زخم بستر وجود ندارد",
        "skin turgor طبیعی و Ecchymosis در ساعد راست مشاهده شد و pressure "
        "injury وجود ندارد.",
        "paragraph", "skin_assessment",
        ("skin turgor", "Ecchymosis", "pressure injury"),
        note="'وجود ندارد' negation preserved verbatim",
    ),
    Case(
        "para_cardiac",
        "مددجو با چست پین تیپیک و تنگی نفس مراجعه و نوار قلب گرفته شد",
        "مددجو با chest pain تیپیک و shortness of breath مراجعه و ECG "
        "گرفته شد.",
        "paragraph", "cardiovascular",
        ("chest pain", "shortness of breath", "ECG"),
    ),
    Case(
        "para_ortho",
        "مددجو با شکستگی استخوان ران راست بستری و کشش پوستی برقرار گردید",
        "مددجو با femur fracture راست بستری و skin traction برقرار گردید.",
        "paragraph", "orthopedic",
        ("femur fracture", "skin traction"),
    ),
    Case(
        "para_mixed_language",
        "بیمار NPO است و IV line برقرار می باشد و vital signs هر چهار ساعت "
        "کنترل می گردد",
        "بیمار NPO است و IV line برقرار می\u200cباشد و vital signs every "
        "four hours کنترل می\u200cگردد.",
        "paragraph", "mixed_language",
        ("NPO", "IV line", "vital signs", "every four hours"),
    ),
)


# ----------------------------------------------------------- H. BiDi cases

#: Logical-order safety: canonical text must never gain direction controls.
BIDI_CASES: tuple[Case, ...] = (
    Case("bidi_mixed_inline",
         "مددجو با شکایت chest pain و BP: 140/85 mmHg مراجعه کرد.",
         "مددجو با شکایت chest pain و BP: 140/85 mmHg مراجعه کرد.",
         "paragraph", "bidi", ("chest pain", "mmHg"), ("140/85",)),
    Case("bidi_vitals_list",
         "علائم حیاتی شامل BP: 140/85 mmHg، SpO2: 97%، Temp: 36.7 °C و "
         "HR: 88 می\u200cباشد.",
         "vital signs شامل BP: 140/85 mmHg، SpO2: 97%، Temp: 36.7 °C و "
         "HR: 88 می\u200cباشد.",
         "paragraph", "bidi", ("vital signs", "mmHg", "°C"), ("140/85", "97", "36.7", "88")),
)


ALL_CASES: tuple[Case, ...] = (
    TERMINOLOGY_CASES
    + SAFETY_CASES
    + NUMBER_CASES
    + TIME_CASES
    + UNIT_CASES
    + GRAMMAR_CASES
    + PARAGRAPH_CASES
    + BIDI_CASES
)


def cases_by_stage() -> dict[str, list[Case]]:
    out: dict[str, list[Case]] = {stage: [] for stage in STAGES}
    for case in ALL_CASES:
        out.setdefault(case.stage, []).append(case)
    return out


def cases_by_group() -> dict[str, list[Case]]:
    out: dict[str, list[Case]] = {}
    for case in ALL_CASES:
        out.setdefault(case.group, []).append(case)
    return out


def dataset_summary() -> dict:
    return {
        "dataset_version": DATASET_VERSION,
        "case_count": len(ALL_CASES),
        "stages": {
            stage: len(cases) for stage, cases in cases_by_stage().items()
        },
        "groups": {
            group: len(cases) for group, cases in sorted(cases_by_group().items())
        },
    }
