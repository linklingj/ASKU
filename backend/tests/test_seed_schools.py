import unittest

from app.models import School
from app.seed_schools import SeedSchool, seed_schools


class FakeStorage:
    def __init__(self, schools: list[School] | None = None):
        self.schools = list(schools or [])
        self.created: list[School] = []
        self.updated: list[tuple[int | None, str, str]] = []

    def list_schools(self):
        return self.schools

    def create_school(self, school: School):
        self.created.append(school)
        self.schools.append(school)
        return school

    def update_school_registration(self, school_id, *, name, base_url):
        self.updated.append((school_id, name, base_url))
        return School(school_id=school_id, name=name, base_url=base_url)


class SeedSchoolsTests(unittest.TestCase):
    def test_seed_creates_only_missing_hosts(self):
        storage = FakeStorage()
        targets = (
            SeedSchool("연세대학교", "https://www.yonsei.ac.kr/notice"),
            SeedSchool("홍익대학교", "https://www.hongik.ac.kr/notice"),
        )

        result = seed_schools(storage, targets)

        self.assertEqual(result, {"created": 2, "updated": 0, "unchanged": 0})
        self.assertEqual([school.name for school in storage.created], ["연세대학교", "홍익대학교"])

    def test_seed_matches_existing_school_by_host_and_preserves_one_row(self):
        storage = FakeStorage([School(school_id=7, name="sejong", base_url="https://www.sejong.ac.kr/old")])

        result = seed_schools(storage, (SeedSchool("세종대학교", "https://www.sejong.ac.kr/notice"),))

        self.assertEqual(result, {"created": 0, "updated": 1, "unchanged": 0})
        self.assertEqual(storage.created, [])
        self.assertEqual(storage.updated, [(7, "세종대학교", "https://www.sejong.ac.kr/notice")])

    def test_seed_is_unchanged_when_registered_data_already_matches(self):
        school = School(school_id=3, name="연세대학교", base_url="https://www.yonsei.ac.kr/notice")
        storage = FakeStorage([school])

        result = seed_schools(storage, (SeedSchool("연세대학교", "https://www.yonsei.ac.kr/notice"),))

        self.assertEqual(result, {"created": 0, "updated": 0, "unchanged": 1})
        self.assertEqual(storage.created, [])
        self.assertEqual(storage.updated, [])
