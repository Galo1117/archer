# -*- coding: UTF-8 -*- 
from .models import users, WorkflowAuditSetting, Group
from django.db.models import Q
from django.conf import settings
from archer.settings import ACCESS_ITOM_ADDR

# leftMenuClass = (
#     {'classname': '', 'name': '基本功能', 'class': '', 'display': ''},
# )

leftMenuBtnsCommon = (
    {'key': 'allworkflow', 'name': 'SQL上线工单', 'url': '/allworkflow/', 'class': 'glyphicon glyphicon-home', 'display': True},
    {'key': 'sqlquery', 'name': 'SQL在线查询', 'url': '/sqlquery/', 'class': 'glyphicon glyphicon-search', 'display': settings.QUERY},
    {'key': 'slowquery', 'name': 'SQL慢查日志', 'url': '/slowquery/', 'class': 'glyphicon glyphicon-align-right', 'display': settings.SLOWQUERY},
    {'key': 'sqladvisor', 'name': 'SQL优化工具', 'url': '/sqladvisor/', 'class': 'glyphicon glyphicon-wrench', 'display': settings.SQLADVISOR},
    {'key': 'queryapply', 'name': '权限查询管理', 'url': '/queryapplylist/', 'class': 'glyphicon glyphicon-eye-open', 'display': settings.QUERY},
)

leftMenuBtnsAliYun = (
    {'key': 'diagnosis', 'name': 'RDS进程管理', 'url': '/diagnosis_process/',
     'class': 'glyphicon glyphicon glyphicon-scissors', 'display': settings.ENABLE_ALIYUN},
)

leftMenuBtnsSuper = (
    {'key': 'config', 'name': '项目配置管理', 'url': '/config/', 'class': 'glyphicon glyphicon glyphicon-option-horizontal', 'display': True},
    {'key': 'admin', 'name': '后台数据管理', 'url': '/admin/', 'class': 'glyphicon glyphicon-hdd', 'display': True},
)

leftMenuBtnsProject = (
    {'key': 'projectresource', 'name': '项目资源管理', 'url': '/projectresource/', 'class': 'glyphicon glyphicon-list', 'display': True},
)

leftMenuBtnsAuditor = (
    {'key': 'workflow', 'name': '所有待办工单', 'url': '/workflow/', 'class': 'glyphicon glyphicon-shopping-cart', 'display': settings.QUERY},
)

leftMenuBtnsDoc = (
    {'key': 'dbaprinciples', 'name': 'SQL审核必读', 'url': '/dbaprinciples/', 'class': 'glyphicon glyphicon-book', 'display': True},
    {'key': 'charts', 'name': '统计图表展示', 'url': '/charts/', 'class': 'glyphicon glyphicon-file', 'display': True},
)

if settings.ENABLE_ALIYUN:
    leftMenuBtnsCommon = leftMenuBtnsCommon + leftMenuBtnsAliYun


def global_info(request):
    """存放用户，会话信息等."""
    loginUser = request.session.get('login_username', None)
    if loginUser is not None:
        user = users.objects.get(username=loginUser)
        # audit_users_list_info = WorkflowAuditSetting.objects.filter().values('audit_users').distinct()
        audit_users_list_info = WorkflowAuditSetting.objects.filter(Q(workflow_type=1) | Q(workflow_type=2)).values('audit_users').distinct()
        project_leaders_list = [ leaders['group_leader'] for leaders in Group.objects.all().values('group_leader').distinct() ]

        audit_users_list = []
        for i in range(len(audit_users_list_info)):
            if ',' in audit_users_list_info[i]['audit_users']:
                audit_users_list += audit_users_list_info[i]['audit_users'].split(',')
            else:
                audit_users_list.append(audit_users_list_info[i]['audit_users'])

        UserDisplay = user.display
        leftMenuBtns = leftMenuBtnsCommon
        if UserDisplay == '':
            UserDisplay = loginUser

        if user.is_superuser:
            leftMenuBtns = leftMenuBtns + leftMenuBtnsProject + leftMenuBtnsAuditor + leftMenuBtnsSuper + leftMenuBtnsDoc

        if loginUser in audit_users_list:
            leftMenuBtns = leftMenuBtns + leftMenuBtnsAuditor

        if loginUser in project_leaders_list:
            leftMenuBtns = leftMenuBtns + leftMenuBtnsProject

    else:
        leftMenuBtns = ()
        UserDisplay = ''

    return {
        'loginUser': loginUser,
        'leftMenuBtns': leftMenuBtns,
        'UserDisplay': UserDisplay,
        'ACCESS_ITOM_ADDR': ACCESS_ITOM_ADDR
    }
