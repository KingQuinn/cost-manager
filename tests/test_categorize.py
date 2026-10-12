"""Rules-first categorizer (architecture.md §7.4) and the v1.8 Drinks split."""
from pathlib import Path

from app.models import DRINK_KEYWORDS, Category
from app.services.categorize import (
    build_default_categories,
    categorize_by_rules,
    categorize_item,
    keyword_tokens,
)


def cats(**kw):
    return [Category(name=name, keywords=words) for name, words in kw.items()]


def defaults():
    return {c.name: c for c in build_default_categories()}


def test_drinks_is_a_default_category():
    d = defaults()
    assert "Drinks" in d and "cola" in d["Drinks"].keywords


def test_live_drink_items_land_in_drinks():
    d = list(defaults().values())
    for name in ["AMERICAN COLA", "AQUAFINA 50CL", "LAWI H2O 485ML (VAR", "LIPTON ICE TEA PEACH 45CL",
                 "Lucozade Boost PET 450M1", "SCHWEPPES CHAPMAN", "CLAY FURA DRINK", "FRESH JUICE 500ML"]:
        assert categorize_by_rules(name, d).name == "Drinks", name


def test_keyword_must_start_a_word():
    c = cats(Drinks=["cola"], Groceries=["chocolate"])
    assert categorize_by_rules("COCA COLA 50CL", c).name == "Drinks"
    assert categorize_by_rules("DAIRY MILK CHOCOLATE", c).name == "Groceries"
    assert categorize_by_rules("BAMA REAL MAYONNAISE", cats(Groceries=["mayonnais"])).name == "Groceries"


def test_longest_keyword_wins_whatever_the_order():
    a = cats(Groceries=["tea"], Drinks=["ice tea"])
    b = cats(Drinks=["ice tea"], Groceries=["tea"])
    assert categorize_by_rules("LIPTON ICE TEA PEACH", a).name == "Drinks"
    assert categorize_by_rules("LIPTON ICE TEA PEACH", b).name == "Drinks"
    assert categorize_by_rules("LIPTON YELLOW LABEL TEA BAGS", a).name == "Groceries"


def test_short_learned_keywords_are_ignored():
    """Live data had 'k', 'g' and 'w' as keywords; with substring matching they claimed almost every item."""
    c = cats(Groceries=["k", "g", "de"], Personal=["w"])
    assert categorize_by_rules("KAIMA HAIR WAX 150G", c) is None


def test_only_meaningful_words_are_learned():
    assert keyword_tokens("LIPTON ICE TEA PEACH 45CL") == ["lipton", "ice", "tea", "peach"]
    assert keyword_tokens("GREEN APPLE PER PACK") == ["apple"]
    assert keyword_tokens("KELLOGGS SPECIAL K 500G #24") == ["kelloggs"]


def test_llm_answer_teaches_only_meaningful_words():
    learned = {}
    c = list(defaults().values())
    cat, used_llm = categorize_item("ZOBITO SPARKLING 33CL", c, llm_fallback=lambda n: "Drinks",
                                    persist_keywords=lambda cid, words: learned.setdefault("w", words))
    assert used_llm and cat.name == "Drinks" and learned["w"] == ["zobito", "sparkling"]


def test_migration_moves_items_with_the_same_drink_keywords():
    sql = Path(__file__).parent.parent.joinpath("supabase/migrations/0005_drinks_category.sql").read_text()
    for kw in DRINK_KEYWORDS:
        assert f"'{kw}'" in sql, kw   # seeded as a Drinks keyword
        assert kw in sql.split("update line_items")[1].split(";")[0], kw  # and used to move old items
