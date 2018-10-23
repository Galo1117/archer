#!/usr/bin/python
# -*- coding: utf-8 -*-
from django.core.paginator import Paginator, EmptyPage, InvalidPage
from django.db import connection
from django.core import serializers
import simplejson as json


def page_list_return(total, current=1):
    """
    page
    分页，返回本次分页的最小页数到最大页数列表
    """
    min_page = current - 2 if current - 4 > 0 else 1
    max_page = min_page + 4 if min_page + 4 < total else total

    return range(min_page, max_page + 1)


def pages(post_objects, request):
    """
    page public function , return page's object tuple
    分页公用函数，返回分页的对象元组
    """
    paginator = Paginator(post_objects, 10)
    try:
        current_page = int(request.GET.get('page', '1'))
    except ValueError:
        current_page = 1

    page_range = page_list_return(len(paginator.page_range), current_page)

    try:
        page_objects = paginator.page(current_page)
    except (EmptyPage, InvalidPage):
        page_objects = paginator.page(paginator.num_pages)

    # 分页器前后翻页按钮功能
    try:
        contacts = paginator.page(current_page)
    except (EmptyPage, InvalidPage):
        contacts = paginator.page(paginator.num_pages)

    if current_page >= 5:
        show_first = 1
    else:
        show_first = 0

    if current_page <= (len(paginator.page_range) - 3):
        show_end = 1
    else:
        show_end = 0

    # 所有对象， 分页器， 本页对象， 所有页码， 本页页码，是否显示第一页，是否显示最后一页，xxx
    return post_objects, paginator, page_objects, page_range, current_page, show_first, show_end, contacts


def ifnull(obj,text):
    '''
    判断字符串是否为空，为空则用text字符替换
    :param obj:
    :return:
    '''
    if obj is None or len(str(obj)) == 0:
        return text
    else:
        return obj


def obj_comparison(obj1, obj2):
    '''
    两数值对象进行比较，输出最后的结果
    :param obj1:
    :param obj2:
    :return:
    '''
    obj1 = int(ifnull(obj1, 0))
    obj2 = int(ifnull(obj2, 0))
    max_boj = max(obj1, obj2)
    min_obj = min(obj1, obj2)
    sum_obj = obj1 + obj2

    if sum_obj == 0:
        result = 0
    elif sum_obj == max_boj:
        result = max_boj
    elif sum_obj > max_boj:
        result = min_obj

    return result


def list_handle(privilegeslist_person, privilegeslist_group):
    '''
    列表数据排序去重处理
    :param privilegeslist_person: 个人权限列表
    :param privilegeslist_group:  所属组权限列表
    :return:
    '''
    # QuerySet 序列化
    privilegeslist_person = serializers.serialize("json", privilegeslist_person)
    privilegeslist_person = json.loads(privilegeslist_person)
    # QuerySet 序列化
    privilegeslist_group = serializers.serialize("json", privilegeslist_group)
    privilegeslist_group = json.loads(privilegeslist_group)

    priv = []
    if len(privilegeslist_person) > 0:
        for i in range(len(privilegeslist_person)):
            privilegeslist_person[i]['fields']['id'] = privilegeslist_person[i]['pk']
            privilegeslist_person[i]['fields']['ascription'] = '个人权限'
            priv.append(privilegeslist_person[i]['fields'])

    if len(privilegeslist_group) > 0:
        for i in range(len(privilegeslist_group)):
            privilegeslist_group[i]['fields']['id'] = privilegeslist_group[i]['pk']
            privilegeslist_group[i]['fields']['ascription'] = '所属组权限'
            priv.append(privilegeslist_group[i]['fields'])

    # 先转字符串保证可以去重排序
    priv = [str(priv) for priv in priv]
    priv = list(set(priv))
    # 去重完成后转成字典
    priv = [eval(priv) for priv in priv]
    # 排序
    privilegeslistAll = sorted(priv, key=lambda priv: (priv['cluster_name'],priv['db_name'],priv['table_name']), reverse=False)
    # 添加用户信息
    return privilegeslistAll


class ServerError(Exception):
    """
    self define exception
    自定义异常
    """
    def __init__(self,message):
        Exception.__init__(self)
        self.message=message


def exeUpdate(sql):  # 更新语句，可执行update,insert语句
    cursor = connection.cursor()
    sta = cursor.execute(sql)
    return (sta)

def exeDelete(sql):  # 删除语句，可批量删除
    cursor = connection.cursor()
    sta = cursor.execute(sql)
    return (sta)

def exeQuery(sql):  # 查询语句
    cursor = connection.cursor()
    cursor.execute(sql)
    desc = cursor.description
    sta = [ dict(zip([col[0] for col in desc], row)) for row in cursor.fetchall() ]
    return (sta)

def connClose():  # 关闭游标
    cursor = connection.cursor()
    cursor.close()

def connCommit():  # 数据提交
    cursor = connection.cursor()
    cursor.commit()
    cursor.close()

def connRollback():  # 数据回滚
    cursor = connection.cursor()
    cursor.rollback()
    cursor.close()
