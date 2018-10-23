#!/bin/bash

#收集所有的静态文件到STATIC_ROOT
python3 manage.py collectstatic -v0 --noinput

settings=${1:-"archer.settings"}
ip=${2:-"127.0.0.1"}
port=${3:-8080}
log_path="/data/dbom/var"
echo "${settings} ${ip} ${port}"

/usr/local/python/bin/gunicorn -w 2 --env DJANGO_SETTINGS_MODULE=${settings} --error-logfile=${log_path}/dbom_err.log --access-logfile=${log_path}/dbom_access.log -b ${ip}:${port} --daemon archer.wsgi:application