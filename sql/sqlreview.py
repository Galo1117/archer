# -*- coding: UTF-8 -*-
import simplejson as json

import time
from threading import Thread

from django.db import connection
from django.utils import timezone
from django.conf import settings

from .dao import Dao
from .const import Const, WorkflowDict
from .sendmail import MailSender
from .inception import InceptionDao
from .aes_decryptor import Prpcrypt
from .models import users, workflow, master_config, slave_config, ProjectResource
from .projectresource import integration_resource, sqlreview_new_createtable
from .workflow import Workflow
from .permission import role_required, superuser_required
import logging

logger = logging.getLogger('default')

dao = Dao()
inceptionDao = InceptionDao()
mailSender = MailSender()
prpCryptor = Prpcrypt()
workflowOb = Workflow()


# 获取当前请求url
def getDetailUrl(request):
    scheme = request.scheme
    host = request.META['HTTP_HOST']
    return "%s://%s/detail/" % (scheme, host)


# 根据实例名获取主库连接字符串，并封装成一个dict
def getMasterConnStr(clusterName):
    listMasters = master_config.objects.filter(cluster_name=clusterName)

    masterHost = listMasters[0].master_host
    masterPort = listMasters[0].master_port
    masterUser = listMasters[0].master_user
    masterPassword = prpCryptor.decrypt(listMasters[0].master_password)
    dictConn = {'masterHost': masterHost, 'masterPort': masterPort, 'masterUser': masterUser,
                'masterPassword': masterPassword}
    return dictConn


# SQL工单跳过inception执行回调
def execute_skipinc_call_back(workflowId, clusterName, sql_content, url):
    workflowDetail = workflow.objects.get(id=workflowId)
    # 获取审核人
    reviewMan = workflowDetail.review_man

    # 获取实例连接信息
    masterInfo = getMasterConnStr(clusterName)
    try:
        # 执行sql
        t_start = time.time()
        execute_result = dao.mysql_execute(masterInfo['masterHost'], masterInfo['masterPort'], masterInfo['masterUser'],
                                           masterInfo['masterPassword'], sql_content)
        t_end = time.time()
        execute_time = "%5s" % "{:.4f}".format(t_end - t_start)
        execute_result['execute_time'] = execute_time + 'sec'

        workflowDetail = workflow.objects.get(id=workflowId)
        if execute_result.get('Warning'):
            workflowDetail.status = Const.workflowStatus['exception']
        elif execute_result.get('Error'):
            workflowDetail.status = Const.workflowStatus['exception']
        else:
            workflowDetail.status = Const.workflowStatus['finish']
        workflowDetail.finish_time = timezone.now()
        workflowDetail.execute_result = json.dumps(execute_result)
        workflowDetail.is_manual = 1
        workflowDetail.audit_remark = ''
        workflowDetail.is_backup = '否'
        # 关闭后重新获取连接，防止超时
        connection.close()
        workflowDetail.save()
    except Exception as e:
        logger.error(e)

    # 如果执行完毕了，则根据settings.py里的配置决定是否给提交者和DBA一封邮件提醒，DBA需要知晓审核并执行过的单子
    if getattr(settings, 'MAIL_ON_OFF'):
        engineer = workflowDetail.engineer
        workflowStatus = workflowDetail.status
        workflowName = workflowDetail.workflow_name
        strTitle = "SQL上线工单执行完毕 # " + str(workflowId)
        strContent = "发起人：" + engineer + "\n审核人：" + reviewMan + "\n工单地址：" + url \
                     + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus
        # 邮件通知申请人，审核人，抄送DBA
        notify_users = reviewMan.split(',')
        notify_users.append(engineer)
        listToAddr = [email['email'] for email in users.objects.filter(username__in=notify_users).values('email')]
        listCcAddr = [email['email'] for email in users.objects.filter(role='DBA').values('email')]
        mailSender.sendEmail(strTitle, strContent, listToAddr, listCcAddr=listCcAddr)


# SQL工单执行回调
def execute_call_back(workflowId, clusterName, url):
    workflowDetail = workflow.objects.get(id=workflowId)
    # 获取审核人
    reviewMan = workflowDetail.review_man

    finalList = []
    dictConn = getMasterConnStr(clusterName)
    try:
        # 交给inception先split，再执行
        (finalStatus, finalList) = inceptionDao.executeFinal(workflowDetail, dictConn)

        # 封装成JSON格式存进数据库字段里
        strJsonResult = json.dumps(finalList)
        workflowDetail = workflow.objects.get(id=workflowId)
        workflowDetail.execute_result = strJsonResult
        workflowDetail.finish_time = timezone.now()
        workflowDetail.status = finalStatus
        workflowDetail.is_manual = 0
        workflowDetail.audit_remark = ''
        # 关闭后重新获取连接，防止超时
        connection.close()
        workflowDetail.save()
    except Exception as e:
        logger.error(e)

    # 获取项目集群
    listAllCluster = slave_config.objects.all().order_by('cluster_name')
    listAllClusterName = [ str(cluster.cluster_name) for cluster in listAllCluster ]
    # 采取异步回调的方式进行资源整合，防止出现持续执行中的异常
    t = Thread(target=integration_resource, args=(listAllClusterName,))
    t.start()

    # 把新创建的表自动授予个对应的选择的项目组
    cluster_name = clusterName
    group_name = workflowDetail.group_name
    result_list = sqlreview_new_createtable(finalList)
    for result in result_list:
        if result["status"] == 0:
            db_name = result["data"]["db_name"].strip("`")
            table_name = result["data"]["table_name"].strip("`")
            defaults_data = { "cluster_name":cluster_name, "db_name":db_name, "table_name":table_name, "group_list":group_name ,"sync_switch":0 }
            # 添加对应的资源
            ProjectResource.objects.update_or_create(cluster_name=cluster_name,db_name=db_name,table_name=table_name,defaults=defaults_data)
            logger.info("Sqlreview new create table add resource (cluster_name:%s db_name:%s table_name:%s) success."%(cluster_name, db_name, table_name))

    # 如果执行完毕了，则根据settings.py里的配置决定是否给提交者和DBA一封邮件提醒，DBA需要知晓审核并执行过的单子
    if getattr(settings, 'MAIL_ON_OFF'):
        # 给申请人，DBA各发一封邮件
        engineer = workflowDetail.engineer
        workflowStatus = workflowDetail.status
        workflowName = workflowDetail.workflow_name
        strTitle = "SQL上线工单执行完毕 # " + str(workflowId)
        strContent = "发起人：" + engineer + "\n审核人：" + reviewMan + "\n工单地址：" + url \
                     + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus
        # 邮件通知申请人，审核人，抄送DBA
        notify_users = reviewMan.split(',')
        notify_users.append(engineer)
        listToAddr = [email['email'] for email in users.objects.filter(username__in=notify_users).values('email')]
        listCcAddr = [email['email'] for email in users.objects.filter(role='DBA').values('email')]
        mailSender.sendEmail(strTitle, strContent, listToAddr, listCcAddr=listCcAddr)


# 给定时任务执行sql
def execute_job(workflowId, url):
    job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)
    logger.debug('execute_job:' + job_id + ' start')
    workflowDetail = workflow.objects.get(id=workflowId)
    clusterName = workflowDetail.cluster_name
    db_name = workflowDetail.db_name

    # 服务器端二次验证，当前工单状态必须为定时执行过状态
    if workflowDetail.status != Const.workflowStatus['tasktiming']:
        raise Exception('工单不是定时执行状态')

    # 将流程状态修改为执行中，并更新reviewok_time字段
    workflowDetail.status = Const.workflowStatus['executing']
    workflowDetail.reviewok_time = timezone.now()
    try:
        workflowDetail.save()
    except Exception:
        # 关闭后重新获取连接，防止超时
        connection.close()
        workflowDetail.save()
    logger.debug('execute_job:' + job_id + ' executing')
    # 执行之前重新split并check一遍，更新SHA1缓存；因为如果在执行中，其他进程去做这一步操作的话，会导致inception core dump挂掉
    splitReviewResult = inceptionDao.sqlautoReview(workflowDetail.sql_content, workflowDetail.cluster_name, db_name,
                                                   isSplit='yes')
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

def testjobs():
    print ("testjobs:%s,%s")