"""同济校园集成。

这里放的都是**只读**能力：教务成绩与课表、图书馆借阅到期、校内页面监看、
一网通办官方日历订阅、绩点换算。凭据全部留在本机数据目录，不进 .env、不进日志。

接口来源（都有第三方实现或官方页面为证，见 `docs/wiki/guides/tongji.md`）：
* 教务 `1.tongji.edu.cn`：请求头 `X-Token: <sessionid>`
* 图书馆 `webpac.tongji.edu.cn`：`jwt` + `jwtHeader`
* 一网通办 `all.tongji.edu.cn/cal/<id>`：官方 ics 订阅
* 统一认证 `iam.tongji.edu.cn`：校内页面监看用它的 Cookie

注意：`*.tongji.edu.cn` 在校外不可达，工具需要在校园网或学校 VPN 内运行。
"""
from __future__ import annotations

__all__ = ["gpa", "jwc", "library", "calendar_feed", "watches", "session", "reader"]
