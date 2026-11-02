from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from ..models import (
    AcademicCatalog,
    AcademicClassGroup,
    AcademicDepartment,
    AcademicProgramType,
    NtubTimetableEntry,
    NtubTimetableSnapshot,
    OfficialTeachingSyllabus,
    Term,
)


class CleanupOrphanSyllabiTests(TestCase):
    def setUp(self):
        self.term = Term.objects.create(code="115-1")
        catalog = AcademicCatalog.objects.create(version="cleanup-syllabi")
        program = AcademicProgramType.objects.create(
            catalog=catalog, code="4", name_zh="四技")
        department = AcademicDepartment.objects.create(
            catalog=catalog, program_type=program, code="406", name_zh="資管")
        self.class_a = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40630B")
        self.class_b = AcademicClassGroup.objects.create(
            catalog=catalog, department=department, code="40620A")

    def syllabus(self, class_code, course_code, snapshot=None):
        return OfficialTeachingSyllabus.objects.create(
            academic_year="115", semester="1", class_code=class_code,
            official_serial="1", course_code=course_code, snapshot=snapshot)

    def test_dry_run_lists_orphans_without_deleting(self):
        orphan = self.syllabus("40630B", "ORPHAN")
        output = StringIO()
        call_command("cleanup_orphan_syllabi", stdout=output)
        self.assertIn(f"PK={orphan.pk}", output.getvalue())
        self.assertIn("Dry-run", output.getvalue())
        self.assertTrue(OfficialTeachingSyllabus.objects.filter(pk=orphan.pk).exists())

    def test_apply_requires_explicit_confirmation(self):
        orphan = self.syllabus("40630B", "ORPHAN")
        with self.assertRaises(CommandError):
            call_command("cleanup_orphan_syllabi", "--apply")
        self.assertTrue(OfficialTeachingSyllabus.objects.filter(pk=orphan.pk).exists())

    def test_apply_deletes_only_orphans_and_keeps_other_snapshot(self):
        orphan = self.syllabus("40630B", "ORPHAN")
        snapshot = NtubTimetableSnapshot.objects.create(
            term=self.term, class_group=self.class_b)
        entry = NtubTimetableEntry.objects.create(
            snapshot=snapshot, entry_type="course", course_code="KEEP",
            official_serial="1", weekday=1, start_period=1, end_period=2)
        kept = self.syllabus("40620A", "KEEP", snapshot=snapshot)

        call_command(
            "cleanup_orphan_syllabi", "--apply",
            "--confirm=DELETE-ORPHAN-SYLLABI", stdout=StringIO())

        self.assertFalse(OfficialTeachingSyllabus.objects.filter(pk=orphan.pk).exists())
        self.assertTrue(OfficialTeachingSyllabus.objects.filter(pk=kept.pk).exists())
        self.assertTrue(NtubTimetableEntry.objects.filter(pk=entry.pk).exists())

    def test_matching_timetable_entry_excludes_unbound_legacy_syllabus(self):
        snapshot = NtubTimetableSnapshot.objects.create(
            term=self.term, class_group=self.class_a)
        NtubTimetableEntry.objects.create(
            snapshot=snapshot, entry_type="course", course_code="MATCH",
            official_serial="1", weekday=1, start_period=1, end_period=2)
        legacy = self.syllabus("40630B", "MATCH")
        output = StringIO()
        call_command("cleanup_orphan_syllabi", stdout=output)
        self.assertNotIn(f"PK={legacy.pk}", output.getvalue())
        self.assertTrue(OfficialTeachingSyllabus.objects.filter(pk=legacy.pk).exists())
