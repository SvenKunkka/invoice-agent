# 来源与设计依据（2026-11-06 核验）


官方买家路径：

- [淘宝买家申请及商家处理](https://developer.alibaba.com/docs/doc.htm?articleId=109001&docType=1&treeId=410)
- [淘宝消费者发票申请路径](https://developer.alibaba.com/docs/doc.htm?articleId=118843&docType=1&treeId=410)（较旧，不能据此保证当前 DOM）
- [淘系订单详情发票回传](https://developer.alibaba.com/docs/doc.htm?articleId=104874&docType=1&treeId=410)
- [京东自助服务/我的发票](https://help.jd.com/user/service.html)
- [京东普通买家下载及专票资质](https://help.jd.com/user/issue/502-247.html)
- [京东可开专票范围](https://help.jd.com/user/issue/509-805.html)（不采用页面中已过期年份的品类税务规则）
- [京东企业集中开票](https://help.jd.com/user/issue/502-4146.html)（不能直接套到个人账号）

开源核查（仅参考结构和页面线索，独立编写本项目）：

- [mydaoyuan/jd-invoice](https://github.com/mydaoyuan/jd-invoice)：无 LICENSE，未复制源码；未开票申请分支只有日志。可参考发票列表入口，不能算专票申请闭环。
- [HaonanYu123/JD-Taobao-MCP](https://github.com/HaonanYu123/JD-Taobao-MCP)：MIT；浏览器持久会话、商品浏览框架，不含订单开票业务。
- [jieyangxchen/parcel-pilot-mcp](https://github.com/jieyangxchen/parcel-pilot-mcp)：MIT；淘宝物流订单 DOM 线索，京东实际页面选择器尚需验证。

运行库一手文档：[Playwright 持久浏览器](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[FastAPI 后台任务](https://fastapi.tiangolo.com/tutorial/background-tasks/)、[Fernet 加密](https://cryptography.io/en/latest/fernet/)。

独立审查修复：申请快照必须在事务中捕获；扫描读改写加事务避免覆盖人工确认；未知专票草稿不自动转普票；提交前实时检查退款/金额/可用专票；下载重新核对购买方；扫描不把当前列表末页当全部历史覆盖。
