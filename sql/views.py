# -*- coding: UTF-8 -*- 

import re, time
import simplejson as json
from threading import Thread
from collections import OrderedDict

from django.db.models import Q, F
from django.db import connection, transaction
from django.utils import timezone
from django.conf import settings
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse, HttpResponseRedirect
from django.core.urlresolvers import reverse
from django.views.decorators.csrf import csrf_exempt

from .dao import Dao
from .api import ServerError, pages
from .const import Const, WorkflowDict
from .inception import InceptionDao
from .aes_decryptor import Prpcrypt
from .models import users, UserGroup, master_config, AliyunRdsConfig, workflow, slave_config, QueryPrivileges, Group, \
    QueryPrivilegesApply, ProjectResource, GroupQueryPrivileges
from .workflow import Workflow
from .permission import role_required, superuser_required
from .sqlreview import getDetailUrl, execute_call_back, execute_skipinc_call_back
from .jobs import job_info, del_sqlcronjob
from .pycrypt import MyCrypt
from .projectresource import integration_resource, get_resource , PermissionVerification, get_query_permisshion
from .query import get_query_clustername
from archer.settings import HASH_KEY
import logging

logger = logging.getLogger('default')

dao = Dao()
inceptionDao = InceptionDao()
prpCryptor = Prpcrypt()
workflowOb = Workflow()


# 登录
def login(request):
    access_itom_addr = settings.ACCESS_ITOM_ADDR
    return HttpResponseRedirect('%s/login/'%(access_itom_addr))
    # return render(request, 'login.html')


# 退出登录
def logout(request):
    access_itom_addr = settings.ACCESS_ITOM_ADDR
    if request.session.get('login_username', False):
        del request.session['login_username']
    if request.session.get('resource_status', False):
        del request.session['resource_status']
    return HttpResponseRedirect('%s/logout/'%(access_itom_addr))
    # return render(request, 'login.html')


# SQL上线工单页面
def allworkflow(request):
    context = {'currentMenu': 'allworkflow'}
    return render(request, 'allWorkflow.html', context)


# 提交SQL的页面
def submitSql(request):
    # 获取数据连接信息
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')

    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    pv = PermissionVerification(loginUser, loginUserOb)
    # 获取用户所属项目组信息
    context = pv.get_group_info()
    if context["status"] == 1:
        group_list = context["data"]
    else:
        errMsg = context["msg"]
        return render(request, 'error.html', {'errMsg': errMsg})

    # 获取用户所属项目组拥有权限的实列信息
    context = pv.get_cluster_info(masters)
    if context["status"] == 1:
        listAllClusterName = context["data"]
    else:
        errMsg = context["msg"]
        return render(request, 'error.html', {'errMsg': errMsg})

    # 获取所有有效用户，通知对象
    active_user = users.objects.filter(is_active=1)

    context = {'currentMenu': 'allworkflow', 'listAllClusterName': listAllClusterName,
               'active_user': active_user, 'group_list': group_list}
    return render(request, 'submitSql.html', context)


# 提交SQL给inception进行解析
def autoreview(request):
    workflowid = request.POST.get('workflowid')
    sqlContent = request.POST['sql_content']
    workflowName = request.POST['workflow_name']
    group_name = request.POST['group_name']
    group_id = Group.objects.get(group_name=group_name).group_id
    clusterName = request.POST['cluster_name']
    db_name = request.POST.get('db_name')
    isBackup = request.POST['is_backup']
    reviewMan = request.POST.get('workflow_auditors')
    notify_users = request.POST.getlist('notify_users')

    # 服务器端参数验证
    if sqlContent is None or workflowName is None or clusterName is None or db_name is None or isBackup is None or reviewMan is None:
        context = {'errMsg': '页面提交参数可能为空'}
        return render(request, 'error.html', context)

    # 删除注释语句
    sqlContent = ''.join(
        map(lambda x: re.compile(r'(^--.*|^/\*.*\*/;[\f\n\r\t\v\s]*$)').sub('', x, count=1),
            sqlContent.splitlines(1))).strip()
    # 去除空行
    sqlContent = re.sub('[\r\n\f]{2,}', '\n', sqlContent)

    if sqlContent[-1] != ";":
        context = {'errMsg': "SQL语句结尾没有以;结尾，请后退重新修改并提交！"}
        return render(request, 'error.html', context)

    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    pv = PermissionVerification(loginUser, loginUserOb)

    # 检测用户资源权限
    if loginUserOb.is_superuser:
        reviewResult = pv.check_resource_priv(sqlContent, clusterName, db_name, 1)
    else:
        reviewResult = pv.check_resource_priv(sqlContent, clusterName, db_name, 0)

    result = reviewResult["data"]
    if reviewResult["status"] == 1:
        context = {'errMsg': reviewResult["msg"]}
        return render(request, 'error.html', context)

    if result is None or len(result) == 0:
        context = {'errMsg': 'inception返回的结果集为空！可能是SQL语句有语法错误'}
        return render(request, 'error.html', context)

    # 要把result转成JSON存进数据库里，方便SQL单子详细信息展示
    jsonResult = json.dumps(result)

    # 遍历result，看是否有任何自动审核不通过的地方，一旦有，则为自动审核不通过；没有的话，则为等待人工审核状态
    workflowStatus = Const.workflowStatus['manreviewing']
    for row in result:
        if row[2] == 2:
            # 状态为2表示严重错误，必须修改
            workflowStatus = Const.workflowStatus['autoreviewwrong']
            break
        elif re.match(r"\w*comments\w*", row[4]):
            workflowStatus = Const.workflowStatus['autoreviewwrong']
            break

    # 调用工作流生成工单
    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 存进数据库里
            engineer = request.session.get('login_username', False)
            if not workflowid:
                Workflow = workflow()
                Workflow.create_time = timezone.now()
            else:
                Workflow = workflow.objects.get(id=int(workflowid))
            Workflow.workflow_name = workflowName
            Workflow.group_id = group_id
            Workflow.group_name = group_name
            Workflow.engineer = engineer
            Workflow.review_man = reviewMan
            Workflow.status = workflowStatus
            Workflow.is_backup = isBackup
            Workflow.review_content = jsonResult
            Workflow.cluster_name = clusterName
            Workflow.db_name = db_name
            Workflow.sql_content = sqlContent
            Workflow.execute_result = ''
            Workflow.audit_remark = ''
            Workflow.save()
            workflowId = Workflow.id
            # 自动审核通过了，才调用工作流
            if workflowStatus == Const.workflowStatus['manreviewing']:
                # 调用工作流插入审核信息, 查询权限申请workflow_type=2
                # 抄送通知人
                listCcAddr = [email['email'] for email in
                              users.objects.filter(username__in=notify_users).values('email')]
                workflowOb.addworkflowaudit(request, WorkflowDict.workflow_type['sqlreview'], workflowId,
                                            listCcAddr=listCcAddr)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)

    return HttpResponseRedirect(reverse('sql:detail', kwargs={'workflowId':workflowId, 'workflowType':0}))


# 展示SQL工单详细内容，以及可以人工审核，审核通过即可执行
def detail(request, workflowId, workflowType):
    workflowDetail = get_object_or_404(workflow, pk=workflowId)
    if workflowDetail.status in (Const.workflowStatus['finish'], Const.workflowStatus['exception']) \
            and workflowDetail.is_manual == 0:
        listContent = json.loads(workflowDetail.execute_result)
    else:
        listContent = json.loads(workflowDetail.review_content)

    # 获取审核人
    reviewMan = workflowDetail.review_man
    reviewMan = reviewMan.split(',')

    # 获取当前审核人
    try:
        current_audit_user = workflowOb.auditinfobyworkflow_id(workflow_id=workflowId,
                                                               workflow_type=WorkflowDict.workflow_type['sqlreview']
                                                               ).current_audit_user
    except Exception:
        current_audit_user = None

    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取定时执行任务信息
    if workflowDetail.status == Const.workflowStatus['tasktiming']:
        job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)
        job = job_info(job_id)
        if job:
            run_date = job.next_run_time
        else:
            run_date = ''
    else:
        run_date = ''

    # sql结果
    column_list = ['ID', 'stage', 'errlevel', 'stagestatus', 'errormessage', 'SQL', 'Affected_rows', 'sequence',
                   'backup_dbname', 'execute_time', 'sqlsha1']
    rows = []
    for row_index, row_item in enumerate(listContent):
        row = {}
        row['ID'] = row_index + 1
        row['stage'] = row_item[1]
        row['errlevel'] = row_item[2]
        row['stagestatus'] = row_item[3]
        row['errormessage'] = row_item[4]
        row['SQL'] = row_item[5]
        row['Affected_rows'] = row_item[6]
        row['sequence'] = row_item[7]
        row['backup_dbname'] = row_item[8]
        row['execute_time'] = row_item[9]
        row['sqlsha1'] = row_item[10]
        rows.append(row)

        if workflowDetail.status == '执行中':
            row['stagestatus'] = ''.join(
                ["<div id=\"td_" + str(row['ID']) + "\" class=\"form-inline\">",
                 "   <div class=\"progress form-group\" style=\"width: 80%; height: 18px; float: left;\">",
                 "       <div id=\"div_" + str(row['ID']) + "\" class=\"progress-bar\" role=\"progressbar\"",
                 "            aria-valuenow=\"60\"",
                 "            aria-valuemin=\"0\" aria-valuemax=\"100\">",
                 "           <span id=\"span_" + str(row['ID']) + "\"></span>",
                 "       </div>",
                 "   </div>",
                 "   <div class=\"form-group\" style=\"width: 10%; height: 18px; float: right;\">",
                 "       <form method=\"post\">",
                 "           <input type=\"hidden\" name=\"workflowid\" value=\"" + str(workflowDetail.id) + "\">",
                 "           <button id=\"btnstop_" + str(row['ID']) + "\" value=\"" + str(row['ID']) + "\"",
                 "                   type=\"button\" class=\"close\" style=\"display: none\" title=\"停止pt-OSC进程\">",
                 "               <span class=\"glyphicons glyphicons-stop\">&times;</span>",
                 "           </button>",
                 "       </form>",
                 "   </div>",
                 "</div>"])
    context = {'currentMenu': 'allworkflow', 'workflowDetail': workflowDetail, 'column_list': column_list, 'rows': rows,
               'reviewMan': reviewMan, 'current_audit_user': current_audit_user, 'loginUserOb': loginUserOb,
               'run_date': run_date}

    if int(workflowType) == 1:
        return render(request, 'detailhash.html', context)
    else:
        return render(request, 'detail.html', context)


# 审核通过，不执行
def passonly(request):
    workflowId = request.POST['workflowid']
    workflowType = request.POST.get('workflowtype',0)
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)
    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)

    # 获取审核人
    reviewMan = workflowDetail.review_man
    reviewMan = reviewMan.split(',')

    # 服务器端二次验证，正在执行人工审核动作的当前登录用户必须为审核人. 避免攻击或被接口测试工具强行绕过
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    if loginUser is None or (loginUser not in reviewMan and loginUserOb.is_superuser != 1):
        context = {'errMsg': '当前登录用户不是审核人，请重新登录.'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，当前工单状态必须为等待人工审核
    if workflowDetail.status != Const.workflowStatus['manreviewing']:
        context = {'errMsg': '当前工单状态不是等待人工审核中，请刷新当前页面！'}
        return render(request, 'error.html', context)

    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 调用工作流接口审核
            # 获取audit_id
            audit_id = workflowOb.auditinfobyworkflow_id(workflow_id=workflowId,
                                                         workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
            auditresult = workflowOb.auditworkflow(request, audit_id, WorkflowDict.workflow_status['audit_success'],
                                                   loginUser, '')

            # 按照审核结果更新业务表审核状态
            if auditresult['data']['workflow_status'] == WorkflowDict.workflow_status['audit_success']:
                # 将流程状态修改为审核通过，并更新reviewok_time字段
                workflowDetail.status = Const.workflowStatus['pass']
                workflowDetail.reviewok_time = timezone.now()
                workflowDetail.audit_remark = ''
                workflowDetail.save()
    except Exception as msg:
        context = {'errMsg': msg}
        if int(workflowType) == 1:
            return HttpResponse(context['errMsg'])
        else:
            return render(request, 'error.html', context)

    return HttpResponseRedirect(reverse('sql:detail', kwargs={'workflowId':workflowId, 'workflowType':workflowType}))


# 仅执行SQL
def executeonly(request):
    workflowId = request.POST['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)

    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)
    clusterName = workflowDetail.cluster_name
    db_name = workflowDetail.db_name
    url = getDetailUrl(request) + str(workflowId) + '/'

    # 获取审核人
    reviewMan = workflowDetail.review_man
    reviewMan = reviewMan.split(',')

    # 服务器端二次验证，正在执行人工审核动作的当前登录用户必须为审核人或者提交人. 避免攻击或被接口测试工具强行绕过
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    if loginUser is None or (loginUser not in reviewMan and loginUser != workflowDetail.engineer and loginUserOb.role != 'DBA'):
        context = {'errMsg': '当前登录用户不是审核人或者提交人，请重新登录.'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，当前工单状态必须为审核通过状态
    if workflowDetail.status != Const.workflowStatus['pass']:
        context = {'errMsg': '当前工单状态不是审核通过，请刷新当前页面！'}
        return render(request, 'error.html', context)

    # 将流程状态修改为执行中，并更新reviewok_time字段
    workflowDetail.status = Const.workflowStatus['executing']
    workflowDetail.reviewok_time = timezone.now()
    # 执行之前重新split并check一遍，更新SHA1缓存；因为如果在执行中，其他进程去做这一步操作的话，会导致inception core dump挂掉
    try:
        splitReviewResult = inceptionDao.sqlautoReview(workflowDetail.sql_content, workflowDetail.cluster_name, db_name,
                                                       isSplit='yes')
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    workflowDetail.review_content = json.dumps(splitReviewResult)
    try:
        workflowDetail.save()
    except Exception:
        # 关闭后重新获取连接，防止超时
        connection.close()
        workflowDetail.save()

    # 采取异步回调的方式执行语句，防止出现持续执行中的异常
    t = Thread(target=execute_call_back, args=(workflowId, clusterName, url))
    t.start()

    return HttpResponseRedirect(reverse('sql:detail', kwargs={ 'workflowId':workflowId, 'workflowType':0 }))


# 跳过inception直接执行SQL，只是为了兼容inception不支持的语法，谨慎使用
@role_required(('DBA',))
def execute_skipinc(request):
    workflowId = request.POST['workflowid']

    # 获取工单信息
    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)
    sql_content = workflowDetail.sql_content
    clusterName = workflowDetail.cluster_name
    url = getDetailUrl(request) + str(workflowId) + '/'

    # 服务器端二次验证，当前工单状态必须为自动审核不通过
    if workflowDetail.status not in [Const.workflowStatus['manreviewing'], Const.workflowStatus['pass'],
                                     Const.workflowStatus['autoreviewwrong']]:
        context = {'errMsg': '当前工单状态不是自动审核不通过，请刷新当前页面！'}
        return render(request, 'error.html', context)

    # 更新工单状态为执行中
    workflowDetail = workflow.objects.get(id=workflowId)
    workflowDetail.status = Const.workflowStatus['executing']
    workflowDetail.reviewok_time = timezone.now()
    workflowDetail.save()

    # 采取异步回调的方式执行语句，防止出现持续执行中的异常
    t = Thread(target=execute_skipinc_call_back, args=(workflowId, clusterName, sql_content, url))
    t.start()

    return HttpResponseRedirect(reverse('sql:detail', kwargs={'workflowId':workflowId, 'workflowType':0}))


# 终止流程
def cancel(request):
    workflowId = request.POST['workflowid']
    workflowType = request.POST.get('workflowtype', 0)
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)

    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)

    # 获取审核人
    reviewMan = workflowDetail.review_man
    reviewMan = reviewMan.split(',')

    audit_remark = request.POST.get('audit_remark')
    if audit_remark is None:
        context = {'errMsg': '驳回原因不能为空'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，如果正在执行终止动作的当前登录用户，不是提交人也不是审核人，则异常.
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    if loginUser is None or (loginUser not in reviewMan and loginUser != workflowDetail.engineer and loginUserOb.role != 'DBA'):
        context = {'errMsg': '当前登录用户不是审核人也不是提交人，请重新登录.'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，如果当前单子状态是结束状态，则不能发起终止
    if workflowDetail.status in (
            Const.workflowStatus['abort'], Const.workflowStatus['finish'], Const.workflowStatus['autoreviewwrong'],
            Const.workflowStatus['exception']):
        return HttpResponseRedirect(reverse('sql:detail', kwargs={'workflowId':workflowId, 'workflowType':workflowType}))

    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 调用工作流接口取消或者驳回
            # 获取audit_id
            audit_id = workflowOb.auditinfobyworkflow_id(workflow_id=workflowId,
                                                         workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
            if loginUser == workflowDetail.engineer:
                auditresult = workflowOb.auditworkflow(request, audit_id, WorkflowDict.workflow_status['audit_abort'],
                                                       loginUser, audit_remark)
            else:
                auditresult = workflowOb.auditworkflow(request, audit_id, WorkflowDict.workflow_status['audit_reject'],
                                                       loginUser, audit_remark)
            # 删除定时执行job
            if workflowDetail.status == Const.workflowStatus['tasktiming']:
                job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)
                del_sqlcronjob(job_id)
            # 按照审核结果更新业务表审核状态
            if auditresult['data']['workflow_status'] in (
                    WorkflowDict.workflow_status['audit_abort'], WorkflowDict.workflow_status['audit_reject']):
                # 将流程状态修改为人工终止流程
                workflowDetail.status = Const.workflowStatus['abort']
                workflowDetail.audit_remark = audit_remark
                workflowDetail.save()
    except Exception as msg:
        context = {'errMsg': msg}
        if int(workflowType) == 1:
            return HttpResponse(context['errMsg'])
        else:
            return render(request, 'error.html', context)

    return HttpResponseRedirect(reverse('sql:detail', kwargs={'workflowId':workflowId, 'workflowType':workflowType}))


# 展示回滚的SQL
def rollback(request):
    workflowId = request.GET['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)
    workflowId = int(workflowId)
    try:
        listBackupSql = inceptionDao.getRollbackSqlList(workflowId)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    workflowDetail = workflow.objects.get(id=workflowId)
    workflowName = workflowDetail.workflow_name
    rollbackWorkflowName = "【回滚工单】原工单Id:%s ,%s" % (workflowId, workflowName)
    context = {'listBackupSql': listBackupSql, 'currentMenu': 'sqlworkflow', 'workflowDetail': workflowDetail,
               'rollbackWorkflowName': rollbackWorkflowName}
    return render(request, 'rollback.html', context)


# SQL审核必读
def dbaprinciples(request):
    context = {'currentMenu': 'dbaprinciples'}
    return render(request, 'dbaprinciples.html', context)


# 图表展示
def charts(request):
    context = {'currentMenu': 'charts'}
    return render(request, 'charts.html', context)


# SQL在线查询
def sqlquery(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取所有从库实例名称
    slaves = slave_config.objects.all().order_by('cluster_name')
    if len(slaves) == 0:
        return HttpResponseRedirect('/admin/sql/slave_config/add/')

    #判断是否为管理员
    if loginUserOb.is_superuser:
        listAllClusterName = [ slave.cluster_name for slave in slaves ]
    else:
        listAllClusterName = get_query_clustername(loginUser)

    context = {'currentMenu': 'sqlquery', 'listAllClusterName': listAllClusterName}
    return render(request, 'sqlquery.html', context)


# SQL慢日志
def slowquery(request):
    # 获取所有实例主库名称
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'slowquery', 'tab': 'slowquery', 'cluster_name_list': cluster_name_list}
    return render(request, 'slowquery.html', context)


# SQL优化工具
def sqladvisor(request):
    # 获取所有实例主库名称
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'sqladvisor', 'listAllClusterName': cluster_name_list}
    return render(request, 'sqladvisor.html', context)


# 查询权限申请列表
def queryapplylist(request):
    slaves = slave_config.objects.all().order_by('cluster_name')
    # 获取用户所属项目组信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    groupname_list = [ group['group_name'] for group in UserGroup.objects.filter(user_name=loginUser).values('group_name') ]

    # 获取所有实例从库名称
    listAllClusterName = [slave.cluster_name for slave in slaves]
    if len(slaves) == 0:
        return HttpResponseRedirect('/admin/sql/slave_config/add/')

    # 获取所有项组名称
    # group_list = Group.objects.all().annotate(id=F('group_id'),
    #                                           name=F('group_name'),
    #                                           parent=F('group_parent_id'),
    #                                           level=F('group_level')
    #                                           ).values('id', 'name', 'parent', 'level')
    group_list = Group.objects.filter(group_name__in=groupname_list).annotate(id=F('group_id'),
                                              name=F('group_name'),
                                              parent=F('group_parent_id'),
                                              level=F('group_level')
                                              ).values('id', 'name', 'parent', 'level')

    group_list = [group for group in group_list]
    if len(group_list) == 0 and loginUserOb.is_superuser == False:
        errMsg = '您尚未属于任何项目组，请与管理员联系.'
        return render(request, 'error.html', {'errMsg': errMsg})
    # elif len(group_list) == 0 and loginUserOb.is_superuser == True:
    #     return HttpResponseRedirect('/config/')

    context = {'currentMenu': 'queryapply', 'listAllClusterName': listAllClusterName,
               'group_list': group_list}
    return render(request, 'queryapplylist.html', context)


# 查询权限申请详情
def queryapplydetail(request, apply_id, audit_type):
    workflowDetail = QueryPrivilegesApply.objects.get(apply_id=apply_id)
    # 获取当前审核人
    audit_info = workflowOb.auditinfobyworkflow_id(workflow_id=apply_id,
                                                   workflow_type=WorkflowDict.workflow_type['query'])

    context = {'currentMenu': 'queryapply', 'workflowDetail': workflowDetail, 'audit_info': audit_info}
    if int(audit_type) == 1:
        return render(request, 'queryapplydetailhash.html', context)
    else:
        return render(request, 'queryapplydetail.html', context)


# 用户的查询权限管理
def queryuserprivileges(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    # 获取所有用户
    user_list_person = [ user['user_name'] for user in QueryPrivileges.objects.filter(is_deleted=0).values('user_name').distinct() ]
    group_name_list = [ group['group_name'] for group in GroupQueryPrivileges.objects.all().values('group_name').distinct() ]
    user_list_group = [ user['user_name'] for user in UserGroup.objects.filter(group_name__in=group_name_list).values('user_name').distinct() ]
    user_list = user_list_person + user_list_group
    # 排序去重
    user_list = sorted(list(set(user_list)))
    context = {'currentMenu': 'queryapply', 'user_list': user_list, 'loginUserOb': loginUserOb}
    return render(request, 'queryuserprivileges.html', context)


# 用户的执行权限管理
def executeuserprivileges(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    # 获取所有用户
    user_list = users.objects.all().values("username").distinct()
    context = {'currentMenu': 'queryapply', 'user_list': user_list, 'loginUserOb': loginUserOb}
    return render(request, 'executeuserprivileges.html', context)


# 问题诊断--进程
def diagnosis_process(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取所有实例名称
    masters = AliyunRdsConfig.objects.all().order_by('cluster_name')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'diagnosis', 'tab': 'process', 'cluster_name_list': cluster_name_list,
               'loginUserOb': loginUserOb}
    return render(request, 'diagnosis.html', context)


# 问题诊断--空间
def diagnosis_sapce(request):
    # 获取所有实例名称
    masters = AliyunRdsConfig.objects.all().order_by('cluster_name')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'diagnosis', 'tab': 'space', 'cluster_name_list': cluster_name_list}
    return render(request, 'diagnosis.html', context)


# 获取工作流审核列表
def workflows(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    context = {'currentMenu': 'workflow', "loginUserOb": loginUserOb}
    return render(request, "workflow.html", context)


# 工作流审核列表
def workflowsdetail(request, audit_id):
    # 按照不同的workflow_type返回不同的详情
    auditInfo = workflowOb.auditinfo(audit_id)
    if auditInfo.workflow_type == WorkflowDict.workflow_type['query']:
        return HttpResponseRedirect(reverse('sql:queryapplydetail', kwargs={'apply_id':auditInfo.workflow_id, 'audit_type':0}))
    elif auditInfo.workflow_type == WorkflowDict.workflow_type['sqlreview']:
        return HttpResponseRedirect(reverse('sql:detail', kwargs={'workflowId':auditInfo.workflow_id, 'workflowType':0}))


# 工作流审核列表HASH认证审核
def workflowsdetailhash(request):
    # 用户免登录更加HASH认证快速审核
    # http://192.168.123.110:8080/workflowshash/?timestamp=454545&hash=kkkkkkkk
    timestamp, uuid, audit_id = None, None, None
    dbom_host = request.scheme + "://" + request.get_host() + "/login/"
    timestamp_before = request.GET.get('timestamp', '')
    hash_encode = request.GET.get('hash', '')
    timestamp_after = int(time.time())
    # 解密哈希
    try:
        crypter = MyCrypt(HASH_KEY)
        hash_text = crypter.decrypt(hash_encode)
        hash_text_list = hash_text.split(',')
        timestamp = hash_text_list[0]
        uuid = hash_text_list[1]
        audit_id = hash_text_list[2]
    except Exception as e:
        errMsg = "HASH鉴权失败，请确保HASH值正常。"
        return HttpResponse(errMsg)

    if int(timestamp_before) != int(timestamp) or (int(timestamp_after) - int(timestamp)) > 3600:
        errMsg = "链接已经超过1小时或TIMESTAMP被修改，请登录DBOM(%s)进行审核。" % (dbom_host)
        return HttpResponse(errMsg)

    # 获取用户信息
    loginUserOb = users.objects.get(uuid=uuid)
    login_username = loginUserOb.username
    if not loginUserOb:
        errMsg = "用户鉴权失败，请登录DBOM(%s)进行审核。" % (dbom_host)
        return HttpResponse(errMsg)
    else:
        request.session['login_username'] = login_username
        request.session.set_expiry(300)

    # 按照不同的workflow_type返回不同的详情
    auditInfo = workflowOb.auditinfo(audit_id)

    if auditInfo.workflow_type == WorkflowDict.workflow_type['query']:
        return HttpResponseRedirect(reverse('sql:queryapplydetail', kwargs={'apply_id':auditInfo.workflow_id, 'audit_type':1}))
    elif auditInfo.workflow_type == WorkflowDict.workflow_type['sqlreview']:
        return HttpResponseRedirect(reverse('sql:detail', kwargs={'workflowId':auditInfo.workflow_id, 'workflowType':1}))


# 配置管理
@superuser_required
def config(request):
    # 获取所有项组名称
    group_list = Group.objects.all().annotate(id=F('group_id'),
                                              name=F('group_name'),
                                              parent=F('group_parent_id'),
                                              level=F('group_level'),
                                              leader=F('group_leader')
                                              ).values('id', 'name', 'parent', 'level', 'leader')
    # 获取组的成员数
    for group_name in group_list:
        members_num = UserGroup.objects.filter(group_name=group_name['name']).count()
        group_name['members_num'] = members_num

    group_list = [group for group in group_list]

    # 获取所有用户
    user_list = users.objects.filter(is_active=1).values('username', 'display')
    context = {'currentMenu': 'config', 'group_list': group_list, 'user_list': user_list,
               'WorkflowDict': WorkflowDict}
    group_list, p, groups, page_range, current_page, show_first, show_end, contacts = pages(group_list, request)
    return render(request, 'config.html', locals())

# 配置项目组信息
@csrf_exempt
def configGroup(request):
    context = { 'status': 1, 'msg':'', 'data': {}} # 1是成功，0是失败
    if request.method == "POST":
        operation_type = request.POST.get('operation_type', None)
        project_name = request.POST.get('project_name', None)
        project_auditors = request.POST.get('project_auditors', None)

        if operation_type == "project_add":
            try:
                if not project_name or len(project_name) == 0:
                    msg = u'项目名称不能为空'
                    raise ServerError(msg)
                elif not project_auditors or len(project_auditors) == 0:
                    msg = u'请选择项目负责人'
                    raise ServerError(msg)
            except ServerError as e:
                context['status'] = 0
                context['msg'] = e.message
                logger.error('项目添加出错:%s'%e.message)
            else:
                try:
                    # 添加组信息
                    group_default_dict = { 'group_name': project_name, 'group_leader': project_auditors }
                    group_obj, group_created = Group.objects.get_or_create(group_name=project_name, group_leader=project_auditors, defaults=group_default_dict)
                    logger.info('project add obj: %s created: %s' % (group_obj, group_created))
                    # 添加用户与组对应关系表
                    usergroup_default_dict = { 'group_name': project_name, 'user_name': project_auditors }
                    usergroup_obj, usergroup_created = UserGroup.objects.get_or_create(group_name=project_name, user_name=project_auditors, defaults=usergroup_default_dict)
                    logger.info('Relationship between the project and the user add obj: %s created: %s' % (usergroup_obj, usergroup_created))
                    # 配置项目成员
                    users_list_select_web = request.POST.getlist('users_selected', [])
                    configGroupMembers(project_name, users_list_select_web)

                    context['status'] = 1
                    context['msg'] = '项目组添加成功'
                    logger.info('Project add %s is success.'%project_name)
                except Exception as e:
                    context['status'] = 0
                    serache_result = re.search('Duplicate entry',str(e))
                    if serache_result:
                        context['msg'] = '项目组已经存在'
                    else:
                        context['msg'] = '项目组添加失败'
                    logger.info('Project add %s is failed. { %s }'%(project_name, e))

        elif operation_type == "project_del":
            project_id = request.POST.get('project_id', None)
            project_name = Group.objects.get(group_id=project_id)
            try:
                # 删除组信息
                Group.objects.filter(group_id=project_id).delete()
                # 删除组对应的用户信息
                UserGroup.objects.filter(group_name=project_name.group_name).delete()
                context['status'] = 1
                context['msg'] = '项目组删除成功'
                logger.info('Project %s delete success.' % project_name.group_name)
            except Exception as e:
                context['status'] = 0
                context['msg'] = '项目组删除失败'
                logger.info('Project %s delete failed. { %s }' %(project_name.group_name, e))

        elif operation_type == "get_project":
            project_dic = {}
            get_type = request.POST.get('get_type', None)
            project_id = request.POST.get('project_id', None)
            try:
                if get_type == 'edit':
                    # 项目组信息
                    project_info = Group.objects.get(group_id=project_id)
                    group_name = project_info.group_name
                    user_list = list(users.objects.filter(is_active=1).values('username'))
                    project_dic["group_id"] = project_info.group_id
                    project_dic["group_name"] = group_name
                    project_dic["group_leader"] = project_info.group_leader
                    project_dic["user_list"] = user_list
                else:
                    group_name = ''

                # 项目组成员信息
                user_list_all = [user['username'] for user in list(users.objects.values('username'))]
                user_list_select = [user['user_name'] for user in list(UserGroup.objects.filter(group_name=group_name).values('user_name'))]
                user_list_noselect = [user for user in user_list_all if user not in user_list_select]
                project_dic["user_list_select"] = user_list_select
                project_dic["user_list_noselect"] = user_list_noselect

                context['data'] = project_dic
                context['status'] = 1
                context['msg'] = '获取项目信息成功'
                logger.info('Get project %s info success.' %group_name)
            except Exception as e:
                context['status'] = 0
                context['msg'] = '获取项目信息失败'
                logger.info('Get project info failed. { %s }' %e)

        elif operation_type == "project_edit":
            edit_group_id = request.POST.get('edit_group_id', None)
            edit_project_name = request.POST.get('edit_project_name', None)
            edit_project_auditors = request.POST.get('edit_project_auditors', None)
            try:
                if not edit_project_name or len(edit_project_name) == 0:
                    msg = u'项目名称不能为空'
                    raise ServerError(msg)
                elif not edit_project_auditors or len(edit_project_auditors) == 0:
                    msg = u'请选择项目负责人'
                    raise ServerError(msg)
            except ServerError as e:
                context['status'] = 0
                context['msg'] = e.message
                logger.error('项目更新出错:%s'%e.message)
            else:
                try:
                    # 更新组信息
                    obj, created = Group.objects.update_or_create(group_id=edit_group_id, defaults={"group_name":edit_project_name, "group_leader":edit_project_auditors})
                    logger.info('project update obj: %s created: %s' % (obj, created))
                    # 配置项目成员
                    users_list_select_web = request.POST.getlist('users_selected', [])
                    configGroupMembers(edit_project_name, users_list_select_web)
                    context['status'] = 1
                    context['msg'] = '项目组更新成功'
                    logger.info('Project ID %s update success.' % edit_group_id)
                except Exception as e:
                    context['status'] = 0
                    serache_result = re.search('Duplicate entry', str(e))
                    if serache_result:
                        context['msg'] = '项目组已经存在'
                    else:
                        context['msg'] = '项目组更新失败'
                    logger.info('Project ID %s update failed. { %s }' %(edit_group_id, e))

    return HttpResponse(json.dumps(context), content_type="application/x-www-form-urlencoded")


# 配置项目成员
@csrf_exempt
def configGroupMembers(group_name, users_list_select_web):

    user_list_select = [ user['user_name'] for user in list(UserGroup.objects.filter(group_name=group_name).values('user_name')) ]
    insert_users_list = [ user for user in users_list_select_web if user not in user_list_select ]
    del_users_list = [ user for user in user_list_select if user not in users_list_select_web ]
    # 插入新增
    for user in insert_users_list:
        obj, created = UserGroup.objects.get_or_create(group_name=group_name, user_name=user, defaults={'group_name':group_name, 'user_name':user})
        logger.info('group members insert obj: %s created: %s'%(obj, created))
    logger.info('group members insert data %s'%insert_users_list)
    # 删除剔除
    for user in del_users_list:
        UserGroup.objects.filter(group_name=group_name, user_name=user).delete()
    logger.info('group members delete data %s' % del_users_list)



# 获取项目资源
@csrf_exempt
def projectresource(request):
    currentMenu = 'projectresource'
    context = {'status': 1, 'msg': '', 'data': {}}  # 1是成功，0是失败
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取项目集群
    listAllCluster = slave_config.objects.all().order_by('cluster_name')
    listAllClusterName = [ str(cluster.cluster_name) for cluster in listAllCluster ]

    if request.session.get('resource_status', 0) == 0:
        logger.debug('异步整合现网表资源信息中...')
        # 采取异步回调的方式进行资源整合，防止出现持续执行中的异常
        t = Thread(target=integration_resource, args=(listAllClusterName,))
        t.start()
    request.session['resource_status'] = 1

    # 获取当前用户所管理的项目列表
    if loginUserOb.is_superuser:
        user_project_list = [ group["group_name"] for group in Group.objects.all().values("group_name").distinct() ]
    else:
        user_project_list = [ group["group_name"] for group in Group.objects.filter(group_leader=loginUser).values("group_name").distinct() ]

    if request.method == "POST":
        limitStart = int(request.POST.get('offset',0))
        pageSize = int(request.POST.get('pageSize',0))
        project_name = request.POST.get('project_name',None)
        cluster_name = request.POST.get('cluster_name',None)
        db_name = request.POST.get('db_name',None)
        search = request.POST.get('search',None)
        config_type = request.POST.get('config_type',None)

        if config_type == "change_cluster":
            listDatabase = []
            if cluster_name:
                # 获取实列所有库信息
                listDatabase = [ row['db_name'] for row in list(ProjectResource.objects.filter(cluster_name=cluster_name).values('db_name').distinct()) ]
            return HttpResponse(json.dumps(listDatabase), content_type="application/x-www-form-urlencoded")

        elif config_type == "get_resource":
            resource_id = request.POST.get('resource_id',None)
            project_name = request.POST.get('project_name',None)
            if not project_name or len(project_name) == 0:
                context['status'] = 0
                context['msg'] = '请选择需要获取权限的项目'
            else:
                try:
                    group_list_str = ProjectResource.objects.get(id=resource_id).group_list
                    if len(group_list_str) > 0:
                        group_list_tmp = group_list_str.split(",")
                    else:
                        group_list_tmp = []
                    group_list_tmp.append(project_name)
                    group_list = ','.join(group_list_tmp)
                    # 更新资源列表信息
                    ProjectResource.objects.update_or_create(id=resource_id, defaults={'group_list':group_list})
                    context['status'] = 1
                    context['data'] = group_list
                    logger.info('Get resource ID %s is success.'%resource_id)
                except Exception as e:
                    context['status'] = 0
                    context['msg'] = '资源获取失败'
                    logger.error('Get resource ID %s is filed. { %s }' %(resource_id, e))
            return HttpResponse(json.dumps(context), content_type="application/x-www-form-urlencoded")

        elif config_type == "get_db_all_resource":
            group_name = request.POST.get('group_name', None)
            cluster_name = request.POST.get('cluster_name',None)
            db_name = request.POST.get('db_name', None)
            if not group_name or len(group_name) == 0:
                context['status'] = 0
                context['msg'] = '请选择项目组'
            elif not cluster_name or len(cluster_name) == 0:
                context['status'] = 0
                context['msg'] = '请选择数据库实例'
            elif not db_name or len(db_name) == 0:
                context['status'] = 0
                context['msg'] = '请选择数据库'
            else:
                try:
                    group_info_list = list(ProjectResource.objects.filter(cluster_name=cluster_name, db_name=db_name).values('id', 'group_list'))
                    for group_info in group_info_list:
                        resource_id = group_info['id']
                        group_list_str = group_info['group_list']
                        if len(group_list_str) > 0:
                            group_list_tmp = group_list_str.split(",")
                        else:
                            group_list_tmp = []
                        if group_name not in group_list_tmp:
                            group_list_tmp.append(group_name)
                            group_list = ','.join(group_list_tmp)
                            # 更新资源列表信息
                            ProjectResource.objects.update_or_create(id=resource_id, defaults={'group_list':group_list})
                            context['status'] = 1
                            context['data'] = group_list
                            logger.info('Get resource ID %s is success.'%resource_id)
                    logger.info('Get whole database %s resource is success.' % db_name)
                except Exception as e:
                    context['status'] = 0
                    context['msg'] = '整库资源获取失败'
                    logger.error('Get whole database %s resource is filed. { %s }' %(db_name, e))
            return HttpResponse(json.dumps(context), content_type="application/x-www-form-urlencoded")

        elif config_type == "del_resource":
            resource_id = request.POST.get('resource_id',None)
            project_name = request.POST.get('project_name',None)
            if not project_name or len(project_name) == 0:
                context['status'] = 0
                context['msg'] = '请先选择项目'
            else:
                try:
                    group_list_tmp = (ProjectResource.objects.get(id=resource_id).group_list).split(",")
                    group_list_tmp.remove(project_name)
                    group_list = ','.join(group_list_tmp)
                    ProjectResource.objects.update_or_create(id=resource_id, defaults={'group_list':group_list})
                    context['status'] = 1
                    context['data'] = group_list
                    logger.info('Delete resource ID %s is success.'%resource_id)
                except Exception as e:
                    context['status'] = 0
                    context['msg'] = '资源清除失败'
                    logger.error('Delete resource ID %s is filed. { %s }' %(resource_id, e))
            return HttpResponse(json.dumps(context), content_type="application/x-www-form-urlencoded")

        elif config_type == "del_db_all_resource":
            group_name = request.POST.get('group_name', None)
            cluster_name = request.POST.get('cluster_name', None)
            db_name = request.POST.get('db_name', None)
            if not group_name or len(group_name) == 0:
                context['status'] = 0
                context['msg'] = '请选择项目组'
            elif not cluster_name or len(cluster_name) == 0:
                context['status'] = 0
                context['msg'] = '请选择数据库实例'
            elif not db_name or len(db_name) == 0:
                context['status'] = 0
                context['msg'] = '请选择数据库'
            else:
                try:
                    group_info_list = list(ProjectResource.objects.filter(cluster_name=cluster_name, db_name=db_name).values('id','group_list'))
                    for group_info in group_info_list:
                        resource_id = group_info['id']
                        group_list_str = group_info['group_list']
                        if len(group_list_str) > 0:
                            group_list_tmp = group_list_str.split(",")
                        else:
                            group_list_tmp = []
                        if group_name in group_list_tmp:
                            group_list_tmp.remove(group_name)
                            group_list = ','.join(group_list_tmp)
                            # 更新资源列表信息
                            ProjectResource.objects.update_or_create(id=resource_id, defaults={'group_list': group_list})
                            context['status'] = 1
                            context['data'] = group_list
                            logger.info('Delete resource ID %s is success.' % resource_id)
                    logger.info('Delete whole database %s resource is success.' % db_name)
                except Exception as e:
                    context['status'] = 0
                    context['msg'] = '整库资源清除失败'
                    logger.error('Delete whole database %s resource is filed. { %s }' % (db_name, e))
            return HttpResponse(json.dumps(context), content_type="application/x-www-form-urlencoded")

        else:
            where_list = ['1=1']
            if cluster_name:
                where_list.append('AND cluster_name="%s"'%cluster_name)
            if db_name:
                where_list.append('AND db_name="%s"'%db_name)
            if search:
                where_list.append('AND ( table_name LIKE "%%%s%%" OR group_list LIKE "%%%s%%" )'%(search, search))

            if len(where_list) > 0:
                where_value = ' '.join(where_list)
                table = 'project_resource'
                count_sql = "SELECT COUNT(1) AS rowcount FROM %s WHERE %s;"%(table, where_value)
                row_sql = "SELECT id,cluster_name,db_name,table_name,group_list FROM %s WHERE %s ORDER by id ASC LIMIT %s,%s;"%(table, where_value, limitStart, pageSize)
                # 获取资源信息
                resource_data = get_resource(count_sql, row_sql, project_name)
            else:
                table = 'project_resource'
                count_sql = "SELECT COUNT(1) AS rowcount FROM %s;"%(table)
                row_sql = "SELECT id,cluster_name,db_name,table_name,group_list FROM %s ORDER by id ASC LIMIT %s,%s;"%(table, limitStart, pageSize)
                # 获取资源信息
                resource_data = get_resource(count_sql, row_sql , project_name)

            return HttpResponse(json.dumps(resource_data), content_type="application/x-www-form-urlencoded")

    group_list = Group.objects.all().annotate(id=F('group_id'),
                                              name=F('group_name'),
                                              parent=F('group_parent_id'),
                                              level=F('group_level')
                                              ).values('id', 'name', 'parent', 'level')

    group_list = [group for group in group_list]

    return render(request, 'project_config/get_project_group_resource.html', locals())


# 设置项目组的查询权限
@csrf_exempt
def groupQueryPermission(request):
    currentMenu = 'projectresource'
    context = {'status': 1, 'msg': '', 'data': {}}  # 1是成功，0是失败
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取项目集群
    listAllCluster = slave_config.objects.all().order_by('cluster_name')
    listAllClusterName = [ str(cluster.cluster_name) for cluster in listAllCluster ]

    # 获取当前用户所管理的项目列表
    if loginUserOb.is_superuser:
        user_group_list = [ group["group_name"] for group in Group.objects.all().values("group_name").distinct() ]
    else:
        user_group_list = [ group["group_name"] for group in Group.objects.filter(group_leader=loginUser).values("group_name").distinct() ]

    if request.method == "POST":
        limitStart = int(request.POST.get('offset',0))
        pageSize = int(request.POST.get('pageSize',0))
        group_name = request.POST.get('group_name',None)
        cluster_name = request.POST.get('cluster_name',None)
        db_name = request.POST.get('db_name',None)
        search = request.POST.get('search',None)

        user_group_text = '\"' + '\",\"'.join(user_group_list) + '\"'
        where_list = ['1=1']
        if group_name:
            where_list.append('AND group_name="%s"' % group_name)
        else:
            where_list.append('AND group_name IN (%s)' % user_group_text)
        if cluster_name:
            where_list.append('AND cluster_name="%s"' % cluster_name)
        if db_name:
            where_list.append('AND db_name="%s"' % db_name)
        if search:
            where_list.append('AND ( table_name LIKE "%%%s%%" OR group_name LIKE "%%%s%%" )' % (search, search))

        where_value = ' '.join(where_list)
        table = 'group_query_privileges'
        count_sql = "SELECT COUNT(1) AS rowcount FROM %s WHERE %s;" % (table, where_value)
        row_sql = "SELECT privilege_id,group_name,cluster_name,db_name,table_name,valid_date,limit_num FROM %s WHERE %s ORDER by privilege_id ASC LIMIT %s,%s;" % (
        table, where_value, limitStart, pageSize)
        # 获取资源信息
        resource_data = get_query_permisshion(count_sql, row_sql)
        # logger.debug('获取权限资源信息:%s.'%resource_data)

        return HttpResponse(json.dumps(resource_data), content_type="application/x-www-form-urlencoded")

    return render(request, 'project_config/set_group_query_permission.html', locals())


# 设置项目组的查询权限
@csrf_exempt
def getGroupQueryPermission(request):
    context = {'status': 1, 'msg': '', 'data': {}}  # 1是成功，0是失败
    group_name = request.POST.get('group_name', None)
    cluster_name = request.POST.get('cluster_name', None)
    db_name = request.POST.get('db_name', None)
    operation_type = request.POST.get('operation_type', None)
    valid_date = request.POST.get('valid_date', None)
    limit_num = request.POST.get('limit_num', 1000)

    table_resource_list = [ table['table_name'] for table in ProjectResource.objects.filter(cluster_name=cluster_name,db_name=db_name).values('table_name') ]

    permission_table_list = [ table['table_name'] for table in GroupQueryPrivileges.objects.filter(group_name=group_name,cluster_name=cluster_name,db_name=db_name).values('table_name') ]
    no_permission_table_list = [ table_name for table_name in table_resource_list if table_name not in permission_table_list ]

    if operation_type == 'resource_save':
        try:
            if not group_name or len(group_name) == 0:
                msg = u'请选择项目组'
                raise ServerError(msg)
            elif not cluster_name or len(cluster_name) == 0:
                msg = u'请选择数据库实列'
                raise ServerError(msg)
            elif not db_name or len(db_name) == 0:
                msg = u'请选择数据库'
                raise ServerError(msg)
            elif not valid_date or len(valid_date) == 0:
                msg = u'请选择授权时间'
                raise ServerError(msg)
            elif not limit_num or len(limit_num) == 0:
                msg = u'请选择查询限制行数'
                raise ServerError(msg)
        except ServerError as e:
            context['status'] = 0
            context['msg'] = e.message
            logger.error('Group premission set error:%s' % e.message)
        else:
            try:
                web_permission_table_list = request.POST.getlist('tables_selected', [])
                new_permission_table_list = [ table_name for table_name in web_permission_table_list if table_name not in permission_table_list ]
                del_permission_table_list = [ table_name for table_name in permission_table_list if table_name not in web_permission_table_list ]
                defaults_data = {'group_name': group_name, 'cluster_name': cluster_name, 'db_name': db_name, 'valid_date': valid_date, 'limit_num': limit_num}
                # 添加新增数据
                for table_name in new_permission_table_list:
                    defaults_data['table_name'] = table_name
                    # 插入数据
                    GroupQueryPrivileges.objects.create(**defaults_data)
                    logger.debug('Insert group query permission %s.' % new_permission_table_list)
                # 删除排除的数据
                for table_name in del_permission_table_list:
                    # 删除数据
                    GroupQueryPrivileges.objects.filter(group_name=group_name,cluster_name=cluster_name,db_name=db_name,table_name=table_name).delete()
                    logger.debug('Delete group query permission %s.' % del_permission_table_list)
                logger.debug('Save group query permission success.%s'%web_permission_table_list)
            except Exception as e:
                context['status'] = 0
                context['msg'] = e
                logger.error('Save group query permission error {%s}.'%e)
    elif operation_type == 'del_premission':
        privilege_id = request.POST.get('privilege_id', None)
        try:
            # 删除对应权限数据
            GroupQueryPrivileges.objects.filter(privilege_id=privilege_id).delete()
            logger.info("Delete group query permission sucdess.")
        except Exception as e:
            context['status'] = 0
            context['msg'] = e
            logger.error('Group premission delete error,:%s' % e)


    table_resource = {}
    table_resource['permission_table_list'] = permission_table_list
    table_resource['no_permission_table_list'] = no_permission_table_list
    context['data'] = table_resource

    return HttpResponse(json.dumps(context), content_type="application/x-www-form-urlencoded")

