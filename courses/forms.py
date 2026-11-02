"""學籍設定表單：學制 → 系所 → 年級 → 班級 的四階級聯。

選項全部來自資料庫（AcademicProgramType / AcademicDepartment / AcademicClassGroup），
程式碼裡不寫死任何系所代碼或班級代碼。

伺服器端不信任前端下拉選單：POST 時會依送上的上層值重新縮小查詢範圍，
所以「送一個不屬於該系所的班級」會在下拉選項驗證階段就被擋掉；
班級與年級的一致性另外在 clean() 再檢查一次。
"""

from django import forms

from .models import AcademicClassGroup, AcademicDepartment, AcademicProgramType


def program_types():
    """可選的學制（有效且開放註冊）。"""
    return AcademicProgramType.objects.filter(is_active=True, is_registration_selectable=True)


def departments():
    """可選的系所（有效且開放註冊；其學制也要有效且開放註冊）。"""
    return AcademicDepartment.objects.filter(
        is_active=True,
        is_registration_selectable=True,
        program_type__is_active=True,
        program_type__is_registration_selectable=True,
    )


def class_groups():
    """可選的班級（有效且開放註冊；上層系所與學制也要有效且開放註冊）。"""
    return AcademicClassGroup.objects.filter(
        is_active=True,
        is_registration_selectable=True,
        department__is_active=True,
        department__is_registration_selectable=True,
        department__program_type__is_active=True,
        department__program_type__is_registration_selectable=True,
    )


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AcademicSetupForm(forms.Form):
    """學籍設定表單：四階級聯（學制／系所／年級／班級）。"""

    program_type = forms.ModelChoiceField(queryset=program_types(), label="學制")
    department = forms.ModelChoiceField(queryset=departments(), label="系所")
    grade = forms.ChoiceField(label="年級", choices=[])
    class_group = forms.ModelChoiceField(queryset=class_groups(), label="班級")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound:
            # 已送出表單：依送上的上層值縮小下層選項範圍，
            # 讓「不屬於該系所的班級」在欄位驗證階段就被拒絕。
            program_type_id = _as_int(self.data.get("program_type"))
            department_id = _as_int(self.data.get("department"))
            dept_qs = departments()
            group_qs = class_groups()
            if program_type_id is not None:
                dept_qs = dept_qs.filter(program_type_id=program_type_id)
            if department_id is not None:
                group_qs = group_qs.filter(department_id=department_id)
            self.fields["department"].queryset = dept_qs
            self.fields["class_group"].queryset = group_qs
        self.grade_choices = sorted({g.grade_no for g in class_groups() if g.grade_no})
        self.fields["grade"].choices = [(str(g), f"{g} 年級") for g in self.grade_choices]

    def clean_grade(self):
        """年級必須是資料庫裡實際存在的年級。"""
        grade = _as_int(self.cleaned_data.get("grade"))
        if grade is None:
            raise forms.ValidationError("請選擇年級。")
        return grade

    def clean(self):
        """再確認班級與系所、年級一致（前端下拉選單不算數）。"""
        cleaned = super().clean()
        group = cleaned.get("class_group")
        department = cleaned.get("department")
        program_type = cleaned.get("program_type")
        grade = cleaned.get("grade")
        if group is None:
            return cleaned
        if department is not None and group.department_id != department.pk:
            self.add_error("class_group", "所選班級不屬於這個系所。")
        if program_type is not None and group.department.program_type_id != program_type.pk:
            self.add_error("class_group", "所選班級不屬於這個學制。")
        if grade is not None and group.grade_no != grade:
            self.add_error("grade", "年級與班級不一致。")
        return cleaned


def selected_values(form):
    """表單目前的選取值（字串），給模板比較用。

    模板的 if 沒辦法呼叫 form.value('...')（智慧 if 解析不了帶括號的方法呼叫），
    所以在這裡先算好，模板只需要比對字串。
    """
    out = {}
    for name in ("program_type", "department", "grade", "class_group"):
        value = form[name].value()
        out[name] = "" if value is None else str(value)
    return out


def cascade_context():
    """給模板用的四階級聯選項資料（全部來自資料庫）。"""
    types = list(program_types())
    depts = list(departments())
    groups = list(class_groups())
    grades = {}
    for g in groups:
        if not g.grade_no:
            continue
        grades.setdefault(g.grade_no, set()).add(g.department_id)
    return {
        "program_types": types,
        "departments": depts,
        "grades": [
            {
                "value": g,
                "label": f"{g} 年級",
                "department_ids": sorted(grades.get(g, set())),
            }
            for g in sorted(grades)
        ],
        "class_groups": groups,
    }
