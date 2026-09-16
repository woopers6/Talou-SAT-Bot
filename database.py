import json
import re
import sqlite3
from dataclasses import dataclass
from fractions import Fraction

import scraper

DIFFICULTIES = {"E": "Easy", "M": "Medium", "H": "Hard"}
ANSWER_TYPES = {"mcq": "Multiple choice", "spr": "Student-produced response"}


@dataclass(frozen=True)
class Filters:
    section_id: int | None = None
    domain_code: str | None = None
    skill_code: str | None = None
    difficulty: str | None = None
    score_band: int | None = None
    answer_type: str | None = None
    exclude_active: bool = False

    def encode(self):
        return "|".join(
            [
                str(self.section_id or ""),
                self.domain_code or "",
                self.skill_code or "",
                self.difficulty or "",
                str(self.score_band or ""),
                self.answer_type or "",
                "1" if self.exclude_active else "0",
            ]
        )

    @classmethod
    def decode(cls, value):
        parts = (value.split("|") + [""] * 7)[:7]
        return cls(
            section_id=int(parts[0]) if parts[0] else None,
            domain_code=parts[1] or None,
            skill_code=parts[2] or None,
            difficulty=parts[3] or None,
            score_band=int(parts[4]) if parts[4] else None,
            answer_type=parts[5] or None,
            exclude_active=parts[6] == "1",
        )


def _parse_number(value):
    value = value.strip().replace("−", "-").replace(" ", "").lstrip("+")
    if not re.fullmatch(r"-?(\d+(\.\d*)?|\.\d+)(/-?\d+(\.\d*)?)?", value):
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/")
            return Fraction(numerator) / Fraction(denominator)
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None


@dataclass
class Question:
    question_id: str
    external_id: str | None
    ibn: str | None
    section_id: int
    section: str
    domain_code: str
    domain: str
    skill_code: str
    skill: str
    difficulty: str
    score_band: int | None
    answer_type: str | None
    active: bool
    legacy: bool
    updated_at: int | None
    content: dict

    @property
    def difficulty_name(self):
        return DIFFICULTIES.get(self.difficulty, "Easy")

    @property
    def choice_letters(self):
        if self.answer_type != "mcq":
            return []
        if self.legacy:
            return [key.upper() for key in (self.content.get("answer") or {}).get("choices", {})]
        return [chr(65 + index) for index in range(len(self.content.get("answerOptions") or []))]

    @property
    def correct_choices(self):
        if self.legacy:
            choice = (self.content.get("answer") or {}).get("correct_choice")
            return [choice.upper()] if choice else []
        return [answer.upper() for answer in self.content.get("correct_answer") or []]

    @property
    def spr_answers(self):
        if self.legacy:
            return list(((self.content.get("answer") or {}).get("correct_spr") or {}).get("absolute") or [])
        return list(self.content.get("correct_answer") or [])

    def check_spr(self, response):
        cleaned = response.strip().replace(" ", "").replace("−", "-")
        user_number = _parse_number(cleaned)
        for answer in self.spr_answers:
            answer = str(answer).strip().replace(" ", "")
            if cleaned.lower() == answer.lower():
                return True
            answer_number = _parse_number(answer)
            if user_number is not None and answer_number is not None and user_number == answer_number:
                return True
        return False


class QuestionBank:
    def __init__(self, path):
        self.path = path
        scraper.connect(path).close()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def count(self, filters=None):
        where, params = self._where(filters or Filters())
        with self._connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM questions WHERE {where}", params).fetchone()[0]

    def domains(self, section_id=None):
        query = "SELECT DISTINCT domain_code, domain, section_id FROM questions"
        params = []
        if section_id:
            query += " WHERE section_id = ?"
            params.append(section_id)
        with self._connect() as conn:
            return [tuple(row) for row in conn.execute(query + " ORDER BY section_id DESC, domain", params)]

    def skills(self, section_id=None, domain_code=None):
        query = "SELECT DISTINCT skill_code, skill, domain_code, section_id FROM questions WHERE 1 = 1"
        params = []
        if section_id:
            query += " AND section_id = ?"
            params.append(section_id)
        if domain_code:
            query += " AND domain_code = ?"
            params.append(domain_code)
        with self._connect() as conn:
            return [tuple(row) for row in conn.execute(query + " ORDER BY section_id DESC, domain_code, skill", params)]

    def random_question(self, filters):
        where, params = self._where(filters)
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM questions WHERE {where} ORDER BY RANDOM() LIMIT 1", params).fetchone()
        return self._question(row)

    def get(self, question_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM questions WHERE content IS NOT NULL AND question_id = ?", (question_id.strip().lower(),)
            ).fetchone()
        return self._question(row)

    @staticmethod
    def _where(filters):
        clauses = ["content IS NOT NULL"]
        params = []
        for column, value in (
            ("section_id", filters.section_id),
            ("domain_code", filters.domain_code),
            ("skill_code", filters.skill_code),
            ("difficulty", filters.difficulty),
            ("score_band", filters.score_band),
            ("answer_type", filters.answer_type),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if filters.exclude_active:
            clauses.append("active = 0")
        return " AND ".join(clauses), params

    @staticmethod
    def _question(row):
        if row is None:
            return None
        return Question(
            question_id=row["question_id"],
            external_id=row["external_id"],
            ibn=row["ibn"],
            section_id=row["section_id"],
            section=row["section"],
            domain_code=row["domain_code"],
            domain=row["domain"],
            skill_code=row["skill_code"],
            skill=row["skill"],
            difficulty=row["difficulty"],
            score_band=row["score_band"],
            answer_type=row["answer_type"],
            active=bool(row["active"]),
            legacy=bool(row["legacy"]),
            updated_at=row["updated_at"],
            content=json.loads(row["content"]),
        )
