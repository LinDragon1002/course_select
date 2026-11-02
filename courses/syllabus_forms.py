from collections import OrderedDict

from django import forms

from .models import CourseSyllabusSubmission, CourseTeachingSyllabus


WEEK_LABELS = (
    "第一週", "第二週", "第三週", "第四週", "第五週", "第六週",
    "第七週", "第八週", "第九週", "第十週", "第十一週", "第十二週",
    "第十三週", "第十四週", "第十五週", "第十六週", "第十七週",
    "第十八週",
)
WEEK_FIELD_NAMES = tuple(f"week_{week:02d}" for week in range(1, 19))
SYLLABUS_FIELDS = (
    "teaching_target", "teaching_matter", *WEEK_FIELD_NAMES,
    "assessment", "references",
)


class CourseTeachingSyllabusForm(forms.ModelForm):
    week_01 = forms.CharField(label=WEEK_LABELS[0], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_02 = forms.CharField(label=WEEK_LABELS[1], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_03 = forms.CharField(label=WEEK_LABELS[2], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_04 = forms.CharField(label=WEEK_LABELS[3], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_05 = forms.CharField(label=WEEK_LABELS[4], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_06 = forms.CharField(label=WEEK_LABELS[5], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_07 = forms.CharField(label=WEEK_LABELS[6], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_08 = forms.CharField(label=WEEK_LABELS[7], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_09 = forms.CharField(label=WEEK_LABELS[8], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_10 = forms.CharField(label=WEEK_LABELS[9], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_11 = forms.CharField(label=WEEK_LABELS[10], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_12 = forms.CharField(label=WEEK_LABELS[11], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_13 = forms.CharField(label=WEEK_LABELS[12], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_14 = forms.CharField(label=WEEK_LABELS[13], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_15 = forms.CharField(label=WEEK_LABELS[14], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_16 = forms.CharField(label=WEEK_LABELS[15], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_17 = forms.CharField(label=WEEK_LABELS[16], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    week_18 = forms.CharField(label=WEEK_LABELS[17], required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = CourseTeachingSyllabus
        fields = SYLLABUS_FIELDS
        widgets = {
            "teaching_target": forms.Textarea(attrs={"rows": 4}),
            "teaching_matter": forms.Textarea(attrs={"rows": 5}),
            "assessment": forms.Textarea(attrs={"rows": 3}),
            "references": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        initial_progress = self.instance.weekly_progress or {}
        for field_name in WEEK_FIELD_NAMES:
            if not self.is_bound and field_name not in self.initial:
                self.initial[field_name] = initial_progress.get(field_name, "")
        self.fields = OrderedDict(
            (name, self.fields[name]) for name in SYLLABUS_FIELDS
        )

    def save(self, commit=True):
        syllabus = super().save(commit=False)
        syllabus.weekly_progress = {
            field_name: self.cleaned_data[field_name]
            for field_name in WEEK_FIELD_NAMES
        }
        if commit:
            syllabus.save()
            self.save_m2m()
        return syllabus


class CourseSyllabusSubmissionForm(CourseTeachingSyllabusForm):
    class Meta(CourseTeachingSyllabusForm.Meta):
        model = CourseSyllabusSubmission


class SyllabusDocxUploadForm(forms.Form):
    MAX_FILE_SIZE = 5 * 1024 * 1024

    file = forms.FileField(
        label="Word 教學大綱（.docx）",
        widget=forms.ClearableFileInput(attrs={"accept": ".docx"}),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if not uploaded.name.lower().endswith(".docx"):
            raise forms.ValidationError("僅接受 .docx Word 文件，不接受 PDF 或其他格式。")
        if uploaded.size > self.MAX_FILE_SIZE:
            raise forms.ValidationError("檔案不可大於 5 MB。")
        return uploaded
