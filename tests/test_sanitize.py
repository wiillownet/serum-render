from serum_render.discover import sanitize


def test_alphanumeric_untouched():
    assert sanitize("Hello123") == "Hello123"


def test_underscores_and_hyphens_kept():
    assert sanitize("foo_bar-baz") == "foo_bar-baz"


def test_spaces_become_underscore():
    assert sanitize("hello world") == "hello_world"


def test_runs_collapsed():
    assert sanitize("foo   bar") == "foo_bar"


def test_leading_trailing_underscore_stripped():
    assert sanitize("_foo_") == "foo"


def test_leading_trailing_whitespace_stripped():
    assert sanitize("  foo  ") == "foo"


def test_brackets_and_punctuation():
    # Real Serum-preset naming convention: "Name [Author]"
    assert sanitize("Lead [FP]") == "Lead_FP"


def test_unicode_letters_kept():
    # Letters in any script survive; an ASCII-only class turned a CJK-only
    # name into an empty stem and the render was named after its folder.
    assert sanitize("café") == "café"
    assert sanitize("日本語 ピアノ") == "日本語_ピアノ"


def test_symbols_still_replaced():
    assert sanitize("a★b") == "a_b"


def test_mixed_punctuation_collapses():
    assert sanitize("foo!@#$bar") == "foo_bar"


def test_empty_input():
    assert sanitize("") == ""


def test_only_punctuation_produces_empty():
    # All chars replaced + collapsed + stripped -> empty string
    assert sanitize("!!!") == ""


def test_trailing_punctuation_stripped():
    assert sanitize("foo...") == "foo"
