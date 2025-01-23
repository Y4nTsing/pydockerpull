# pydockerpull
直接纯python文件对harbor仓库进行docker pull

直接将harbor中docker pull的链接部分作为参数传入即可，默认支持以下两种格式：
```
<harbor-host>/<project>/<image>:<tag>
<harbor-host>/<project>/<image>/<image-name>@sha256:<digest>
```
usage demo:
```
python3 pull.py x.x.x.x/aaa/bbb@sha256:xxxxxx
```

可指定hostname，避免harbor配置0.0.0.0导致的无法pull的问题。

因为匿名访问也需要传入账号密码，默认使用admin/Harbor12345进行认证，可通过参数修改账号密码。

完成后通过docker load -i xxx.tar 进行镜像加载。
