绝对禁止执行任何会修改 Git 仓库历史或远端状态的命令. 允许执行只读查询命令.

- Keep local verification focused on the files and packages changed. Run the smallest relevant test set; do not run the full workspace test suite as a routine completion step.

# 资深工程师准则
分析需求、制定方案、编写代码前, 必须先触发 `karpathy-guidelines` 技能。



本规则适用于仓库内现有和以后新增的所有 Java 后台服务及 Java 后台模块, 包括插件模块. Java 后台业务代码必须按传统分层结构组织, 标准调用链为:

`Controller -> Service -> ServiceImpl -> Mapper -> Database`

- `controller` 负责 HTTP 接口, 请求解析, 基础参数校验和响应组装, 不直接访问 Mapper, 类名必须以 `Controller` 结尾.
- `service` 负责声明业务服务接口, 类名必须以 `Service` 结尾.
- `service/impl` 负责实现 Service, 承担核心业务规则, 业务校验, 多组件编排和事务边界, 类名必须以 `ServiceImpl` 结尾.
- `mapper` 负责数据库访问, 类名必须以 `Mapper` 结尾. 不涉及数据库访问的功能不要求虚构 Mapper.
- `entity` 负责承载数据库实体或明确的领域数据模型. `dto` 负责请求, 响应和层间传输数据. Controller 不直接把持久化细节当成业务逻辑处理.
- Controller 应依赖 Service, 不应直接依赖 ServiceImpl. ServiceImpl 可以依赖 Mapper 和其他 Service, 但不能让 Controller 绕过 Service 层调用 Mapper.
- 新增或重构一项常规后台业务功能时, 不得使用自定义业务目录绕开上述标准分层. 插件可以保留插件边界, 但插件内部仍按同样的 `controller/service/service/impl/mapper` 结构组织.

`dto`, `entity`, `security`, `common`, `client`, `config`, `listener`, `interceptor`, `spi` 等辅助分层可以按实际职责存在, 不强制套统一后缀, 但不能替代或绕开标准业务分层链路. 不得为了后缀机械添加 `Service`, `Dto`, `Entity` 等名称. 非服务类不能为了放入 `service/impl` 而伪装成 `ServiceImpl`, 应放入与其职责匹配的辅助分层或目录.

移动或重构 Java 后台分层时, 必须同时检查并同步文件名, 类名, 构造器名, `package`, `import`, Spring 注入点, MyBatis XML, 资源配置和测试引用, 不能只移动目录.