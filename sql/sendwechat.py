#!/usr/bin/env python
# -*- coding: utf-8 -*-
import urllib
import json
import sys

class weChat():
    def __init__(self,url,Corpid,Secret):
        url = '%s/cgi-bin/gettoken?corpid=%s&corpsecret=%s' % (url,Corpid,Secret)
        res = self.url_req(url)
        self.token = res['access_token']

    def url_req(self,url,method='get',data={}):
        if method == 'get':
            req = urllib.request.Request(url)
            res = json.loads(urllib.request.urlopen(req).read().decode(encoding="utf8"))
        elif method == 'post':
            req = urllib.request.Request(url,data)
            print ('POST:%s'%req)
            res = json.loads(urllib.request.urlopen(req).read().decode(encoding="utf8"))
        else:
            print ('error request method...exit')
            sys.exit()
        return res

    def send_message(self,userlist,grouplist,content,agentid=0):
        self.userlist = userlist
        self.grouplist = grouplist
        self.content = content
        self.agentid = agentid
        url = 'https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s' % self.token
        data = {
            "touser": "",
            "toparty": "",
            "totag": "",
            "msgtype": "text",
            "agentid": 0,
            "text": {
                "content": ""
            },
            "safe":"0"
        }
        data['touser'] = userlist
        data['toparty'] = grouplist
        data['agentid'] = agentid
        data['text']['content'] = content
        # data = json.dumps(data,ensure_ascii=False)
        data = json.dumps(data).encode('UTF-8')
        res = self.url_req(url,method='post',data=data)
        if res['errmsg'] == 'ok':
            print ('send sucessed!!!')
            return 1
        else:
            print ('send failed!!')
            print (res)
            return 0