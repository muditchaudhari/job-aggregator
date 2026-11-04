"""Named industry title ladders."""

from __future__ import annotations

import pytest

from app.models.enums import SeniorityLevel
from app.normalization.fields import parse_seniority
from app.normalization.ladders import _LADDERS, ladder_names, seniority_from_ladder


class TestBankingGrades:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Analyst", SeniorityLevel.JUNIOR),
            ("Analyst, Credit Risk", SeniorityLevel.JUNIOR),
            ("Senior Analyst", SeniorityLevel.MID),
            ("Associate", SeniorityLevel.MID),
            ("Senior Associate", SeniorityLevel.SENIOR),
            ("Assistant Vice President", SeniorityLevel.SENIOR),
            ("API Software Engineer - AVP", SeniorityLevel.SENIOR),
            ("Vice President", SeniorityLevel.MANAGER),
            ("Executive Director", SeniorityLevel.DIRECTOR),
            ("Managing Director", SeniorityLevel.DIRECTOR),
        ],
    )
    def test_grades_map_onto_our_levels(self, title, expected) -> None:
        assert seniority_from_ladder(title, "banking") is expected

    def test_longer_phrases_are_checked_first(self) -> None:
        """"senior analyst" must not match on "analyst" and lose the "senior".

        Asserted against the table itself, not just one example, so reordering
        it cannot silently regrade a whole bank.
        """
        for grades in _LADDERS.values():
            for index, (phrase, _level) in enumerate(grades):
                for later, _ in grades[index + 1 :]:
                    # A shorter phrase placed first would shadow the longer
                    # one: "analyst" before "senior analyst" drops the grade.
                    assert phrase not in later, (
                        f"{phrase!r} is contained in {later!r} but comes before it"
                    )

    def test_abbreviations_need_word_boundaries(self) -> None:
        """A substring test finds "md" inside "Commodities"."""
        assert seniority_from_ladder("Commodities Trader", "banking") is SeniorityLevel.UNKNOWN
        assert seniority_from_ladder("Vped Specialist", "banking") is SeniorityLevel.UNKNOWN
        assert seniority_from_ladder("Trader, MD", "banking") is SeniorityLevel.DIRECTOR

    def test_a_tech_title_on_a_bank_board_falls_through(self) -> None:
        """Banks post real engineering titles too; those read normally."""
        assert seniority_from_ladder("Software Engineer III", "banking") is SeniorityLevel.UNKNOWN

    def test_an_unknown_or_absent_ladder_is_a_no_op(self) -> None:
        assert seniority_from_ladder("Analyst", None) is SeniorityLevel.UNKNOWN
        assert seniority_from_ladder("Analyst", "nonsense") is SeniorityLevel.UNKNOWN

    def test_banking_is_registered(self) -> None:
        assert "banking" in ladder_names()


class TestLadderBeatsTheTechWords:
    """Where a ladder applies, our own words read the title *wrongly*.

    Not incompletely — inverted. "Senior Analyst" is a mid-level bank grade;
    the generic matcher sees "senior" and stops, putting it two levels above
    where it belongs and vetoing a reasonable application. "Assistant Vice
    President" reads as a director.
    """

    @pytest.mark.parametrize(
        ("title", "tech", "banking"),
        [
            ("Analyst", SeniorityLevel.UNKNOWN, SeniorityLevel.JUNIOR),
            ("Senior Analyst", SeniorityLevel.SENIOR, SeniorityLevel.MID),
            ("Associate", SeniorityLevel.JUNIOR, SeniorityLevel.MID),
            ("Assistant Vice President", SeniorityLevel.DIRECTOR, SeniorityLevel.SENIOR),
        ],
    )
    def test_the_two_ladders_disagree_and_the_named_one_wins(
        self, title, tech, banking
    ) -> None:
        assert parse_seniority(title) is tech
        assert parse_seniority(title, ladder="banking") is banking

    def test_a_level_suffix_still_wins_when_no_grade_is_present(self) -> None:
        assert parse_seniority("Software Engineer II", ladder="banking") is SeniorityLevel.JUNIOR

    def test_the_description_is_still_the_last_resort(self) -> None:
        got = parse_seniority(
            "Platform Role", "We need 9+ years of experience", ladder="banking"
        )
        assert got is not SeniorityLevel.UNKNOWN


class TestPortalConfig:
    def _write(self, tmp_path, line: str):
        from app.userconfig import load_portals

        path = tmp_path / "portals.txt"
        path.write_text(line + "\n")
        return load_portals(path)

    def test_a_ladder_is_read_from_the_third_field(self, tmp_path) -> None:
        portals = self._write(tmp_path, "https://x.example/jobs | Citi | ladder=banking")

        assert portals[0].name == "Citi"
        assert portals[0].ladder == "banking"

    def test_a_portal_without_a_ladder_still_parses(self, tmp_path) -> None:
        portals = self._write(tmp_path, "https://x.example/jobs | GitLab")

        assert portals[0].name == "GitLab"
        assert portals[0].ladder is None

    def test_an_unknown_ladder_is_rejected_not_ignored(self, tmp_path) -> None:
        """A typo that silently does nothing would show up only as postings
        quietly scored on the wrong scale."""
        from app.userconfig import ConfigError

        with pytest.raises(ConfigError, match="unknown ladder"):
            self._write(tmp_path, "https://x.example/jobs | Citi | ladder=bankng")
