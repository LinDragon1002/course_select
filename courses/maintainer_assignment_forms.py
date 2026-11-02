"""批次安排班級課程（維護員一次指派多門可選課程給一個學生班級）用的表單。

這個模組只負責「收集與驗證輸入」；真正的權限判斷與寫入資料庫都放在
`courses.admin` 裡，兩邊都做伺服器端檢查，不信任任何從瀏覽器傳來的 id。

設計上刻意把「來源開課班別」和「目標學生班級」分開：

* 來源開課班別（Section）：課本來開在哪個班（40620A / 40630A …）都可以。
* 目標學生班級（AcademicClassGroup）：政策要套用到哪個學生班級（40630B …）。

所以**不要求**開課班級與學生班級相同；跨班指派是這支工具的主要用途。
"""

from django import forms

from .maintainer import scoped_class_groups, scoped_sections
from .models import (AcademicClassGroup, AcademicDepartment, AcademicProgramType,
                     Section, SectionClassPolicy, Term)


def available_class_groups(user):
    """這個維護員可以指派的學生班級（落在授權系所底下）。"""
    return scoped_class_groups(user)


def available_sections(user, term):
    """這個維護員在某個學期可以挑來當「來源」的開課班別。

    範圍沿用 `scoped_sections`：與授權班級有關的開課班別（官方曾針對該
    班級開課、該班級已有政策、或官方課綱推薦提到該班級）。

    這裡**不**再過濾「開課班級 == 學生班級」：政策本來就是跨班用的。
    """
    queryset = scoped_sections(user).select_related("course", "course__term")
    if term is None:
        return queryset
    return queryset.filter(course__term=term)


def _section_label(section):
    """勾選清單上顯示的字串：課號、課名、班別、學期。"""
    course = section.course
    return f"{course.code} {course.name}（{section.label} 班，{course.term.code}）"


class BulkClassAssignmentSelectForm(forms.Form):
    """第一步：依學期與四階級聯選擇學生班級。

    下拉選單只給授權系所的班級：別系的班級連選都選不到；就算有人改 POST
    送出別的班級 id，欄位驗證也會直接擋下（queryset 裡沒有這個選項）。
    """

    term = forms.ModelChoiceField(queryset=Term.objects.all(), label="學期",
                                  empty_label="— 請選擇學期 —")
    program_type = forms.ModelChoiceField(queryset=AcademicProgramType.objects.none(),
                                           label="學制", required=False,
                                           empty_label="— 請選擇學制 —")
    department = forms.ModelChoiceField(queryset=AcademicDepartment.objects.none(),
                                         label="系所", required=False,
                                         empty_label="— 請選擇系所 —")
    grade = forms.ChoiceField(label="年級", required=False,
                              choices=[("", "— 請選擇年級 —")])
    class_group = forms.ModelChoiceField(queryset=AcademicClassGroup.objects.none(),
                                          label="學生班級",
                                          empty_label="— 請選擇學生班級 —")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        groups = available_class_groups(user).select_related(
            "department", "department__program_type")
        departments = AcademicDepartment.objects.filter(
            pk__in=groups.values("department_id")).select_related("program_type")
        program_types = AcademicProgramType.objects.filter(
            pk__in=departments.values("program_type_id"))
        self.fields["program_type"].queryset = program_types
        self.fields["department"].queryset = departments
        self.fields["class_group"].queryset = groups
        self.fields["grade"].choices = [("", "— 請選擇年級 —")] + [
            (str(grade), f"{grade} 年級")
            for grade in sorted(set(groups.exclude(grade_no__isnull=True)
                                     .values_list("grade_no", flat=True)))
        ]

        if self.is_bound:
            program_type_id = self.data.get("program_type")
            department_id = self.data.get("department")
            if program_type_id:
                self.fields["department"].queryset = departments.filter(
                    program_type_id=program_type_id)
            if department_id:
                self.fields["class_group"].queryset = groups.filter(
                    department_id=department_id)

    def clean_class_group(self):
        """學生班級必須落在授權系所底下（偽造 id 會在這裡被擋）。"""
        class_group = self.cleaned_data["class_group"]
        if not available_class_groups(self.user).filter(pk=class_group.pk).exists():
            raise forms.ValidationError("你只能替授權系所的學生班級安排課程。")
        return class_group

    def clean(self):
        cleaned = super().clean()
        group = cleaned.get("class_group")
        program_type = cleaned.get("program_type")
        department = cleaned.get("department")
        grade = cleaned.get("grade")
        # 舊連結只帶 term + class_group，允許略過前端級聯欄位。
        if group is None:
            return cleaned
        if department is not None and group.department_id != department.pk:
            self.add_error("class_group", "所選班級不屬於這個系所。")
        if program_type is not None and group.department.program_type_id != program_type.pk:
            self.add_error("class_group", "所選班級不屬於這個學制。")
        if grade:
            try:
                grade_no = int(grade)
            except (TypeError, ValueError):
                self.add_error("grade", "請選擇有效年級。")
            else:
                if group.grade_no != grade_no:
                    self.add_error("grade", "年級與班級不一致。")
        return cleaned


class BulkClassAssignmentForm(forms.Form):
    """第二步：勾選開課班別、選修課分類，批次建立政策。

    這張表單把「學期／學生班級」也一併帶回來做伺服器端檢查（避免有人
    改 POST 內容）；來源開課班別的可選範圍則收斂到授權範圍 + 所選學期。

    建立出來的政策一律是 DRAFT：批次安排不等於發布，發布仍然走既有的
    「發布選定的修課政策」動作。
    """

    term = forms.ModelChoiceField(queryset=Term.objects.all(), label="學期")
    class_group = forms.ModelChoiceField(queryset=AcademicClassGroup.objects.none(),
                                          label="學生班級")
    sections = forms.ModelMultipleChoiceField(
        queryset=Section.objects.none(), widget=forms.CheckboxSelectMultiple,
        label="來源開課班別")
    course_category = forms.ChoiceField(
        choices=(
            (SectionClassPolicy.CourseCategory.DEPARTMENT_ELECTIVE, "專業選修"),
            (SectionClassPolicy.CourseCategory.PROFESSIONAL_ELECTIVE, "跨班專業選修"),
            (SectionClassPolicy.CourseCategory.GENERAL_ELECTIVE, "通識選修"),
        ),
        label="修課分類")
    can_enroll = forms.BooleanField(label="可自行加選", required=False, initial=True,
                                     help_text="發布後學生才可以自行加選這門課。")

    def __init__(self, *args, user, term=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["class_group"].queryset = available_class_groups(user)
        # 可選的來源範圍：維護員有權管理的開課班別（再收斂到所選學期）。
        # 偽造 Section ID 會因為不在 queryset 裡而無法通過欄位驗證。
        self.fields["sections"].queryset = available_sections(user, term)
        # 勾選清單上顯示課號、課名、班別、學期（instance 屬性：不會被當成
        # bound method，多傳一個 self 的問題）。
        self.fields["sections"].label_from_instance = lambda obj: _section_label(obj)

    def clean_sections(self):
        """被勾選的來源開課班別必須屬於所選學期。

        queryset 已經收斂到授權範圍，所以「偽造 Section ID」在欄位驗證階段
        就會被擋（不會寫入任何資料）。
        """
        sections = self.cleaned_data["sections"]
        term = self.cleaned_data.get("term")
        if term is not None:
            wrong_term = [s for s in sections if s.course.term_id != term.pk]
            if wrong_term:
                raise forms.ValidationError("選到的開課班別有課程不屬於所選學期。")
        return sections

    def clean_class_group(self):
        """學生班級必須落在授權系所底下（偽造 id 會在這裡被擋）。"""
        class_group = self.cleaned_data["class_group"]
        if not available_class_groups(self.user).filter(pk=class_group.pk).exists():
            raise forms.ValidationError("你只能替授權系所的學生班級安排課程。")
        return class_group
