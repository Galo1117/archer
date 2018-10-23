# -*- coding: UTF-8 -*- 
from .models import users, WorkflowAuditSetting, Group
from django.db.models import Q
from django.conf import settings
from archer.settings import ACCESS_ITOM_ADDR

# leftMenuClass = (
#     {'menuclass': 'baseFunnction', 'menuname': '基本功能', 'class': 'fa fa-tachometer', 'display': True},
#     {'menuclass': 'projectManage', 'menuname': '项目管理', 'class': 'fa fa-cubes', 'display': True},
#     {'menuclass': 'backConfig', 'menuname': '后台配置', 'class': 'fa fa-gears', 'display': True},
#     {'menuclass': 'helpTools', 'menuname': '帮助工具', 'class': 'fa fa-gavel', 'display': True},
# )

leftMenuBtnsCommon = (
    {'menuclass': 'baseFunnction','key': 'allworkflow', 'name': 'SQL上线工单', 'url': '/allworkflow/', 'class': 'fa fa-cloud-upload', 'display': True},
    {'menuclass': 'baseFunnction','key': 'sqlquery', 'name': 'SQL在线查询', 'url': '/sqlquery/', 'class': 'fa fa-search', 'display': settings.QUERY},
    {'menuclass': 'baseFunnction','key': 'slowquery', 'name': 'SQL慢查日志', 'url': '/slowquery/', 'class': 'fa fa-align-right', 'display': settings.SLOWQUERY},
    {'menuclass': 'baseFunnction','key': 'sqladvisor', 'name': 'SQL优化工具', 'url': '/sqladvisor/', 'class': 'fa fa-wrench', 'display': settings.SQLADVISOR},
    {'menuclass': 'projectManage','key': 'queryapply', 'name': '权限查询管理', 'url': '/queryapplylist/', 'class': 'fa fa-leaf', 'display': settings.QUERY},
)

leftMenuBtnsAliYun = (
    {'id': 6,'key': 'diagnosis', 'name': 'RDS进程管理', 'url': '/diagnosis_process/',
     'class': 'fa fa-scissors', 'display': settings.ENABLE_ALIYUN},
)

leftMenuBtnsSuper = (
    {'menuclass': 'projectManage','key': 'config', 'name': '项目配置管理', 'url': '/config/', 'class': 'fa fa-wrench', 'display': True},
    {'menuclass': 'backConfig','key': 'admin', 'name': '后台数据管理', 'url': '/admin/', 'class': 'fa fa-calculator', 'display': True},
)

leftMenuBtnsProject = (
    {'menuclass': 'projectManage','key': 'projectresource', 'name': '项目资源管理', 'url': '/projectresource/', 'class': 'fa fa-list', 'display': True},
)

leftMenuBtnsAuditor = (
    {'menuclass': 'baseFunnction','key': 'workflow', 'name': '所有待办工单', 'url': '/workflow/', 'class': 'fa fa-shopping-cart', 'display': settings.QUERY},
)

leftMenuBtnsDoc = (
    {'menuclass': 'helpTools','key': 'dbaprinciples', 'name': 'SQL审核必读', 'url': '/dbaprinciples/', 'class': 'fa fa-book', 'display': True},
    {'menuclass': 'helpTools','key': 'charts', 'name': '统计图表展示', 'url': '/charts/', 'class': 'fa fa-file', 'display': True},
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
        # 'leftMenuClass':leftMenuClass,
        'leftMenuBtns': leftMenuBtns,
        'UserDisplay': UserDisplay,
        'ACCESS_ITOM_ADDR': ACCESS_ITOM_ADDR
    }
