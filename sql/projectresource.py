# -*- coding: UTF-8 -*-
from .models import ProjectResource, slave_config, Group, UserGroup
from django.db.models import F
from django.conf import settings
from .aes_decryptor import Prpcrypt
from .inception import InceptionDao
from .api import *
from .dao import Dao
import threading
import logging
import datetime
import re

logger = logging.getLogger('default')
prpCryptor = Prpcrypt()
lock = threading.RLock()
dao = Dao()

# 整合项目资源
def integration_resource(listAllClusterName):
    # 线程加锁
    lock.acquire()
    # 初始化线上资源列表
    online_resource_list = []
    if len(listAllClusterName) > 0:
        # 根据集群获取库信息
        for cluster_name in listAllClusterName:
            slave_info = slave_config.objects.get(cluster_name=cluster_name)
            try:
                listDatabase = dao.getAlldbByCluster(slave_info.slave_host, slave_info.slave_port, slave_info.slave_user,
                                                     prpCryptor.decrypt(slave_info.slave_password))
                logger.info('Get project resource database info %s .'%listDatabase)
            except Exception as e:
                logger.error('Get project resource database info error {%s}.'%e)

            # 根据库获取表信息
            for database_name in listDatabase:
                try:
                    listTable = dao.getAllTableByDb(slave_info.slave_host, slave_info.slave_port, slave_info.slave_user,
                                                    prpCryptor.decrypt(slave_info.slave_password), database_name)
                    logger.info('Get project resource table info %s .' % listTable)
                except Exception as e:
                    logger.error('Get project resource table info error {%s}.' % e)

                # 根据不同的表生成资源信息
                for table_name in listTable:
                    # 初始化资源信息字典
                    resource_dic = {}
                    resource_dic['cluster_name'] = cluster_name
                    resource_dic['db_name'] = database_name
                    resource_dic['table_name']=table_name
                    # 将资源字典信息组成列表
                    online_resource_list.append(resource_dic)

    # 获取DBOM资源列表
    dbom_resource_all_list = list(ProjectResource.objects.values('cluster_name','db_name','table_name'))
    dbom_resource_sync_list = list(ProjectResource.objects.filter(sync_switch=1).values('cluster_name', 'db_name', 'table_name'))


    # 获取差异资源
    # DBOM有现网没有的资源需要删掉
    delete_resource_list = [ resource for resource in dbom_resource_sync_list if resource not in online_resource_list ]
    # 现网有DBOM没有的资源需要插入
    insert_resource_list = [ resource for resource in online_resource_list if resource not in dbom_resource_all_list ]

    for resource in insert_resource_list:
        obj, created = ProjectResource.objects.update_or_create(cluster_name = resource['cluster_name'], db_name = resource['db_name'],table_name = resource['table_name'],
                                                                defaults=resource)
    for resource in delete_resource_list:
        ProjectResource.objects.filter(cluster_name = resource['cluster_name'], db_name = resource['db_name'],table_name = resource['table_name']).delete()

    # 更新所有数据同步开关状态为1
    ProjectResource.objects.all().update(sync_switch=1)

    # 运行完成释放线程锁
    lock.release()

    logger.info("new instart resource :%s"%insert_resource_list)
    logger.info("new delete resource :%s" %delete_resource_list)


# 获取项目资源
def get_resource(count_sql, row_sql, project_name):
    '''
    count_sql: 统计符合条件资源数目SQL
    row_sql： 获取资源详细信息SQL
    '''
    # 初始化信息
    rowCount = [{'rowcount':0}]
    contentResult = []
    if len(project_name) == 0: project_name = "Null"
    # 执行SQL
    try:
        rowCount = exeQuery(count_sql)
        contentResult = exeQuery(row_sql)
        logger.info(u"[项目资源信息]-获取项目资源信息成功")
    except Exception as e:
        logger.error(u"[项目资源信息]-获取项目资源信息失败，失败原因:{ %s }" % e)
    # 关闭游标
    connClose()

    # 循环资源列表，判断项目组是否已经具有该资源，如果有则为1，否则为0
    for resource in contentResult:
        project_list = ifnull(resource['group_list'],",").split(",")
        if project_name in project_list:
            resource["is_in"] = 1
        else:
            resource["is_in"] = 0

    resource_data = {}
    resource_data['total'] = rowCount[0]["rowcount"]
    resource_data["rows"] = contentResult
    # 返回资源信息
    return resource_data


# 获取项目资源
def get_query_permisshion(count_sql, row_sql):
    '''
    count_sql: 统计符合条件权限数目SQL
    row_sql： 获取权限详细信息SQL
    '''
    # 初始化信息
    rowCount = [{'rowcount':0}]
    contentResult = []

    # 执行SQL
    try:
        rowCount = exeQuery(count_sql)
        contentResult = exeQuery(row_sql)
        logger.info(u"[项目权限信息]-获取项目查询权限信息成功")
    except Exception as e:
        logger.error(u"[项目权限信息]-获取项目查询权限信息失败，失败原因:{ %s }" % e)
    # 关闭游标
    connClose()

    # 转换下时间类型
    for i in range(len(contentResult)):
        valid_date = datetime.datetime.strftime(contentResult[i]['valid_date'], '%Y-%m-%d')
        contentResult[i]['valid_date'] = valid_date

    resource_data = {}
    resource_data['total'] = rowCount[0]["rowcount"]
    resource_data["rows"] = contentResult
    # 返回资源信息
    return resource_data


def sqlreview_analysis(finalinfo, type):
    '''
    用于SQL审评，分析执行SQL语句
    '''
    # 执行SQL
    if type == "check":
        exec_sql = " ".join(map(lambda x: re.compile(r'(^--.*|^/\*.*\*/;[\f\n\r\t\v\s]*$)').sub('', x, count=1),finalinfo.splitlines()))
    else:
        exec_sql = " ".join(map(lambda x: re.compile(r'(^--.*|^/\*.*\*/;[\f\n\r\t\v\s]*$)').sub('', x, count=1), finalinfo[5].splitlines()))

    # 以空格间隔解析SQL
    exec_sql_break_list = [breakInfo for breakInfo in exec_sql.split(" ") if breakInfo != ""]
    # 获取SQL的执行命名
    exec_comment_head = "%s %s" % (exec_sql_break_list[0].lower(), exec_sql_break_list[1].lower())
    # 获取SQL执行的结果
    exec_result = finalinfo[3].split("\n")
    # 需要返回的信息
    result = { "exec_sql": exec_sql, "exec_sql_break_list": exec_sql_break_list, "exec_comment_head":exec_comment_head, "exec_result":exec_result }

    # 返回结果信息
    return result


def sqlreview_new_createtable(finalList):
    '''
    用于SQL审评新建表的时候，自动将对应的表资源权限授予给对应的项目组
    :return:
    '''
    result_list = []
    for finalinfo in finalList:
        result = {"status": 0, "msg": "OK", "data": {}}
        analysis_info = sqlreview_analysis(finalinfo, "")
        exec_sql_break_list = analysis_info["exec_sql_break_list"]
        exec_comment_head = analysis_info["exec_comment_head"]
        exec_result = analysis_info["exec_result"]
        # 如果是新建表则将表资源授予给相应的项目
        if exec_comment_head == "create table" and 'Execute Successfully' in exec_result:
            db_name = ""
            table_info = exec_sql_break_list[2]
            logger.debug("Table info %s."%table_info)
            if len(table_info.split(".")) > 1:
                db_name = table_info.split(".")[0]
                table_name = table_info.split(".")[1]
            else:
                exec_comment_index = finalList.index(finalinfo)
                for index in reversed(range(exec_comment_index)):
                    analysis_info = sqlreview_analysis(finalList[index], "")
                    exec_sql_break_list = analysis_info["exec_sql_break_list"]
                    if exec_sql_break_list[0].lower() == "use":
                        db_name = exec_sql_break_list[1]
                        break
                table_name = table_info.split(".")[0]
            result_data = {"db_name": db_name, "table_name": table_name}
            result["data"] = result_data
        else:
            result["status"] = 1
            result["msg"] = "新表创建失败或者该语句不是建表语句."
        # 将处理信息添加到结果集里面
        result_list.append(result)

    # 返回结果信息
    return result_list


class PermissionVerification(InceptionDao):
    '''
    用于验证用户拥有权限信息
    '''
    def __init__(self, loginUser, loginUserOb):
        self.loginUser = loginUser
        self.loginUserOb = loginUserOb

        self.prpCryptor = Prpcrypt()
        self.inception_host = getattr(settings, 'INCEPTION_HOST')
        self.inception_port = int(getattr(settings, 'INCEPTION_PORT'))
        self.inception_remote_backup_host = getattr(settings, 'INCEPTION_REMOTE_BACKUP_HOST')
        self.inception_remote_backup_port = int(getattr(settings, 'INCEPTION_REMOTE_BACKUP_PORT'))
        self.inception_remote_backup_user = getattr(settings, 'INCEPTION_REMOTE_BACKUP_USER')
        self.inception_remote_backup_password = getattr(settings, 'INCEPTION_REMOTE_BACKUP_PASSWORD')


    # 用户所属项目组获取
    def _get_gropu(self):
        project_usergroup_list = [group["group_name"] for group in UserGroup.objects.filter(user_name=self.loginUser).values("group_name").distinct()]
        project_group_list = [group["group_name"] for group in Group.objects.filter(group_leader=self.loginUser).values("group_name").distinct()]
        project_list = list(set(project_usergroup_list + project_group_list))
        return project_list

    # 根据项目组获取项目组其它信息
    def get_group_info(self):
        context = { "status": 1, "msg":"ok", "data":{} }
        project_list = self._get_gropu()
        if len(project_list) == 0 and self.loginUserOb.is_superuser == False:
            context["status"] = 0
            context["msg"] = "您尚未属于任何项目组，请与管理员联系."
        elif self.loginUserOb.is_superuser == True:
            # 获取所有项组名称
            group_list = Group.objects.all().annotate(id=F('group_id'), name=F('group_name'),parent=F('group_parent_id'),
                                                      level=F('group_level')).values('id', 'name', 'parent', 'level')
            group_list = [group for group in group_list]

            if len(group_list) == 0:
                context["status"] = 0
                context["msg"] = '该系统尚未创建任何项目组，请与管理员联系创建.'
            else:
                context["status"] = 1
                context["data"] = group_list
        else:
            group_list = []
            for groupname in project_list:
                # 获取所有项组名称
                group_info = list(Group.objects.filter(group_name=groupname).annotate(id=F('group_id'), name=F('group_name'),
                                                                                      parent=F('group_parent_id'), level=F('group_level')).values('id', 'name','parent','level'))
                group_list = group_info + group_list

            context["status"] = 1
            context["data"] = group_list

        # 返回验证数据
        return context


    # 根据项目组获取项目组拥有的实列信息
    def get_cluster_info(self, masters):
        context = { "status": 1, "msg":"ok", "data":{} }
        project_list = self._get_gropu()
        if len(project_list) == 0 and self.loginUserOb.is_superuser == False:
            context["status"] = 0
            context["msg"] = "您尚未属于任何项目组，请与管理员联系."
        elif self.loginUserOb.is_superuser == True:
            # 获取所有实例名称
            listAllClusterName = [ master.cluster_name for master in masters ]
            if len(listAllClusterName) == 0:
                context["status"] = 0
                context["msg"] = '该系统尚未添加任何实列，请与管理员联系创建.'
            else:
                context["status"] = 1
                context["data"] = listAllClusterName
        else:
            listAllClusterName = []
            for groupname in project_list:
                # 获取所有实例名称
                resource_info = list(
                    ProjectResource.objects.filter(group_list__icontains=groupname).values("cluster_name").distinct())
                if len(resource_info) > 0:
                    listAllClusterName_tmp = [resource["cluster_name"] for resource in resource_info]
                else:
                    listAllClusterName_tmp = []
                listAllClusterName = listAllClusterName_tmp + listAllClusterName

            # 不同组可能拥有同一个实列，所以需要去重
            listAllClusterName = list(set(listAllClusterName))
            context["status"] = 1
            context["data"] = listAllClusterName

        # 返回验证数据
        return context


    # 根据项目组获取项目组拥有的库信息
    def get_db_info(self, clustername):
        context = { "status": 1, "msg":"ok", "data":{} }
        if self.loginUserOb.is_superuser == True:
            # 获取所有实例名称
            listDbName = [ dbinfo["db_name"] for dbinfo in ProjectResource.objects.filter(cluster_name=clustername).values("db_name").distinct() ]
            context["status"] = 1
            context["data"] = listDbName
        else:
            listDbName = []
            project_list = self._get_gropu()
            for groupname in project_list:
                # 获取所有实例名称
                resource_info = [ resource["db_name"] for resource in ProjectResource.objects.filter(cluster_name=clustername, group_list__icontains=groupname).values("db_name").distinct() ]
                listDbName = resource_info + listDbName

            # 不同组可能拥有同一个实列，所以需要去重
            listDbName = list(set(listDbName))
            context["status"] = 1
            context["data"] = listDbName

        # 返回验证数据
        return context


    # 根据项目组获取项目组拥有的库信息
    def get_table_info(self, clustername, dbname):
        context = { "status": 1, "msg":"ok", "data":{} }
        if self.loginUserOb.is_superuser == True:
            # 获取所有实例名称
            listTableName = [ dbinfo["db_name"] for dbinfo in ProjectResource.objects.filter(cluster_name=clustername, db_name=dbname).values("db_name").distinct() ]
            context["status"] = 1
            context["data"] = listTableName
        else:
            listTableName = []
            project_list = self._get_gropu()
            for groupname in project_list:
                # 获取所有实例名称
                resource_info = [ resource["db_name"] for resource in ProjectResource.objects.filter(cluster_name=clustername, db_name=dbname, group_list__icontains=groupname).values("db_name").distinct() ]
                listTableName = resource_info + listTableName

            # 不同组可能拥有同一个实列，所以需要去重
            listTableName = list(set(listTableName))
            context["status"] = 1
            context["data"] = listTableName

        # 返回验证数据
        return context


    # 核验当前用户是否具有提交的SQL的所有权限
    def check_resource_priv(self, sqlContent, clusterName, dbName, userType=0):
        reviewResult = { "status": 0, "msg":"ok", "data":{} }
        project_list = self._get_gropu()
        # 获取组的资源
        resource_list = []
        for groupname in project_list:
            resource_info = [ "%s:%s"%(resource["db_name"],resource["table_name"]) for resource in ProjectResource.objects.filter(cluster_name=clusterName, group_list__icontains=groupname).values("db_name", "table_name").distinct() ]
            resource_list = resource_info + resource_list
        # 对权限列表进行去除、排序
        priv_resource_list = list(set(resource_list))

        # 交给inception进行自动审核
        try:
            result = self.sqlautoReview(sqlContent, clusterName, dbName)
        except Exception as e:
            result = []
            reviewResult['status'] = 1
            reviewResult['msg'] = str(e) + '\n请参照安装文档配置pymysql'
            logger.error("%s\n请参照安装文档配置pymysql" % e)

        # 将结果数据转成列表格式
        result_list = [ list(i) for i in result ]
        reviewResult['data'] = result_list

        # 如果非管理员用户，需要对权限做验证
        if userType == 0:
            # 定义SQL操作类型列表
            ddl_comment_list = ["alter"]
            dml_comment_list = ["insert", "delete", "update"]
            # 循环分析执行数据，对不同SQL执行权限做对比
            try:
                for finalinfo in result_list:
                    # 获取执行SQL
                    exec_sql = "%s;"% finalinfo[5]
                    # 获取执行语句对应的执行数据库名称
                    db_name = dbName
                    exec_comment_index = result_list.index(finalinfo)
                    for index in reversed(range(exec_comment_index)):
                        analysis_result = sqlreview_analysis(result_list[index], "")
                        exec_sql_break_index_list = analysis_result["exec_sql_break_list"]
                        if exec_sql_break_index_list[0].lower() == "use":
                            db_name = exec_sql_break_index_list[1]
                            break

                    # logger.debug("sqlautoReview:%s" % result_list)
                    # 如果执行语句为ALTER语句
                    analysis_result = sqlreview_analysis(finalinfo, "")
                    exec_sql_break_list = analysis_result["exec_sql_break_list"]
                    if exec_sql_break_list[0].lower() in ddl_comment_list:
                        table_info = exec_sql_break_list[2].split(".")
                        if len(table_info) > 1:
                            curr_resource= "%s:%s"%(table_info[0], table_info[1])
                        else:
                            curr_resource= "%s:%s"%(db_name, table_info[0])

                        curr_resource = curr_resource.replace('`', '')
                        if curr_resource not in priv_resource_list and finalinfo[2] != 2:
                            finalinfo[2] = 2
                            finalinfo[4] = "Table %s has no permissions."%(curr_resource.replace(':','.'))
                    elif exec_sql_break_list[0].lower() in dml_comment_list:
                        # 打印语法树
                        query_result = self.query_print(exec_sql, clusterName, db_name)
                        query_result_list = list(query_result[0])
                        logger.debug(query_result)
                        if query_result_list[2] == 0:
                            # 判断row_item[3]的数据类型
                            try:
                                query_result_info = eval(query_result_list[3])
                            except Exception as e:
                                query_result_info = {}

                            if isinstance(query_result_info, list) or isinstance(query_result_info, dict):
                                table_info = query_result_info
                            else:
                                table_info = {}
                            # 单表操作返回为table_object,多表管理返回为table_ref
                            if "table_object" in table_info.keys():
                                # 由于DELETE/UPDATE/INSERT返回的table_object类型不一样，所以需要做一下判断
                                if isinstance(table_info["table_object"], list) == True:
                                    resource_info = table_info["table_object"]
                                elif isinstance(table_info["table_object"], dict) == True:
                                    resource_info = [table_info["table_object"]]
                            elif "table_ref" in table_info.keys():
                                resource_info = table_info["table_ref"]
                            # 获取所有库：表权限列表
                            resource_list = [ "%s:%s" % (resource["db"], resource["table"]) for resource in resource_info ]
                            logger.debug(resource_list)
                            # 对权限列表进行去除、排序
                            curr_resource_list = list(set(resource_list))

                            # 对执行SQL解析出db&table资源信息进行权限判断
                            errormessage = []
                            for resource in curr_resource_list:
                                resource = resource.replace('`', '')
                                if resource not in priv_resource_list:
                                    errormessage.append("Table %s has no permissions." % (resource.replace(':','.')))

                            if len(errormessage) > 0:
                                finalinfo[2] = 2
                                finalinfo[4] = "\n".join(errormessage)
            except Exception as e:
                reviewResult['status'] =  1
                reviewResult['msg'] = "SQL执行权限验证异常,{ %s }."%e

        # 传回处理数据
        return reviewResult