SYLLABUS_TEACHER_GROUP_NAME = "Syllabus Teacher"
SYLLABUS_TEACHER_DISPLAY_NAME = "任課教師（課綱提交）"


def has_teacher_syllabus_access(user):
    if not user or not user.is_authenticated or not user.is_active or not user.is_staff:
        return False

    from .maintainer import is_maintainer, is_superuser

    if is_superuser(user) or is_maintainer(user):
        return True
    return user.groups.filter(name=SYLLABUS_TEACHER_GROUP_NAME).exists()
