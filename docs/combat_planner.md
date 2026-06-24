# Combat Planner 开发指南

Planner 是队伍大脑。角色只声明两类信息：

- 我能尝试做什么：`ActionIntent`
- 我需要队友怎么配合：`CombatContext.request_*()` / `reserve_actions()`

Planner 负责切人、动作执行顺序、协作路线、动作保留、入场/环合反应、以及没有动作可做时的站场兜底。

公开导入入口固定使用：

```python
from src.combat.planner import ActionSlot, CombatContext, FieldClaim
```

也可以使用 compact namespace，减少角色文件里的 enum import 数量：

```python
from src.combat.planner import Planner

self.planner_action(
    tags={Planner.ActionTag.SKILL_ACTION},
    execute=self.some_action,
    chain_policy=Planner.EntryChainPolicy.STOP_ON_SUCCESS,
)

context.reserve_actions(
    holds,
    until=Planner.NEVER_EXPIRES,
)
```

直接导入仍是一等 API，适合少量枚举或类型注解场景：

```python
from src.combat.planner import EntryChainPolicy
```

`src.combat.planner` 只导出正式开发 API。角色作者应只依赖这些名字：

- 统一命名空间：`Planner`
- 基础画像：`Role`、`RoleProfile`、`FieldPreference`
- 动作声明：`ActionIntent`、`ActionResult`、`ActionTag`、`ActionSlot`、`EntryChainPolicy`
- 入场诉求：`FieldClaim`、`FieldClaimLevel`、`ExpectedEntry`
- 协作建模：`FollowupStep`、`ActionReservation`、`NEVER_EXPIRES`
- 执行上下文：`CombatContext`
- 切入保护：`SwitchInGuard`
- 任务集成：`CombatPlanner`、`SwitchDecision`

不要从 facade 依赖评分表、request lifecycle 类型别名、或 `planner/requests.py`
中的内部请求类型；这些都是 planner 实作细节，后续可以自由重构。

内部实现已经拆成 package：

- `planner/__init__.py`：对外 facade；角色开发者只从这里导入。
- `planner/core.py`：`CombatPlanner` 核心调度。
- `planner/types.py`：正式 API 类型、enum、ActionIntent、FieldClaim、FollowupStep。
- `planner/context.py`：角色与 planner 沟通的 `CombatContext`。
- `planner/requests.py`：route / reservation / tag request 的内部实现。
- `planner/state.py`：运行状态、route 进度、request 生命周期。

角色代码不要直接导入内部模块；这样 planner 内部可以继续重构，而角色 API 保持稳定。

## 快速入口

普通角色通常只需要覆盖 `describe_role()` 和 `combat_intents(context)`：

```python
def describe_role(self):
    return RoleProfile(
        role=Role.SUB_DPS,
        field_preference=FieldPreference.SUB_DPS,
        max_field_time=1.5,
    )

def combat_intents(self, context: CombatContext):
    return self.intents(
        self.click_ultimate_action(),
        self.click_skill_action(),
    )
```

角色常用 API：

- `click_ultimate_action()`：声明 Q。
- `click_skill_action()`：声明 E。
- `planner_action()`：声明自定义入口动作。
- `FieldClaim.normal/high/critical(...)`：声明“我现在应该被切进来”。
- `context.request_route(...)`：动作成功后发布固定协作路线。
- `context.request_switch(...)`：动作成功后请求下一次普通调度切给某角色。
- `context.reserve_actions(...)`：动作成功后或 policy 中保留队友动作。
- `context.can_execute_action(...)`：执行长动作期间询问 planner 是否允许某个槽位。

## RoleProfile

`describe_role()` 返回 `RoleProfile`，用于描述角色的基础画像。

```python
RoleProfile(
    role=Role.SUB_DPS,
    field_preference=FieldPreference.SUB_DPS,
    max_field_time=1.5,
    combat_start_priority=0,
)
```

字段：

- `role`：角色定位，目前主要用于描述和未来扩展；常用值是 `MAIN_DPS`、`SUB_DPS`、`SUPPORT`。
- `field_preference`：普通评分中的站场偏好。主 DPS 会在没有协作请求时更容易被切出来；`SETUP_ONLY` 会降低普通站场倾向。
- `max_field_time`：该角色从登场到离场的站场预算。Q/E 已经消耗的时间会计入预算，planner 只补剩余 `field_time`。
- `combat_start_priority`：开战首切优先级。大于 0 才参与首切；普通战斗切人不使用此字段。

## ActionIntent

`ActionIntent` 表达“角色进场后可以尝试做什么”。不要把一次普攻、等待、连点等内部细节拆成很多 action；这些应写在一个 action 的 `execute` 内。

字段：

- `tags: set[ActionTag]`：动作意义和评分依据。必须是 set，例如 `{ActionTag.SKILL_ACTION}`。
- `execute: Callable[[CombatContext], ActionResult | bool | None]`：真正执行动作的函数。
- `name: str = ""`：高级精确匹配和日志名。普通自定义动作可以留空；Q/E helper 会自动生成。
- `slot: ActionSlot | None = None`：动作槽位。协作路线和 reservation 优先用 slot 匹配。
- `reason: str = ""`：planner 日志和切人理由。
- `can_execute: Callable[[CombatContext], bool] | None`：planner 层硬限制。返回 False 时动作不会执行，也不会参与评分。
- `priority_ready: Callable[[CombatContext], bool] | None`：只用于切人评分。返回 False 表示“不值得为了这个 action 切人”，但角色已经在场时仍可能尝试该 action。
- `chain_policy: EntryChainPolicy = EntryChainPolicy.CONTINUE`：动作执行后是否继续本次入场。

如果 action 设置了 `slot`，planner 会自动通过 `context.can_execute_action(...)`
检查 reservation。开发者传入的 `can_execute` 只需要表达额外机制限制，不要重复写
reservation 查询。

`execute` 返回规则非常严格：

- 返回 `True`：成功。
- 返回 `False`：失败。
- 返回 `None`：失败。
- 没写 `return`：等同 `None`，失败。
- 返回 `ActionResult`：使用 `ActionResult.success`。
- 返回 `1`、`"ok"`、`(True, 0.1)` 这类 truthy 值不会被当成成功。

普通角色不需要手写 `ActionResult`。只有需要自定义 result name/tags/slot/reason
时才手写。协作请求不要放进 `ActionResult`；动作执行中直接调用
`context.request_route()`、`context.reserve_actions()`
或 `context.request_switch()`。

## ActionTag

`ActionTag` 表达动作意义和评分，不能表达某个角色专属机制。

常用标签：

- `ULTIMATE_ACTION`：Q。已经包含 Q 的默认输出价值。
- `SKILL_ACTION`：E。已经包含 E 的默认输出价值。
- `ARC_ACTION`：弧盘动作，评分为 0。`BaseChar.perform()` 会统一先按一次 arc，
  普通角色不需要把 arc 放进 planner 动作链。
- `SUPPORT`：辅助/治疗/增益类动作。
- `COORDINATION`：发布协作路线或窗口的动作。
- `COORDINATION_FINISHER`：协作完成后的收尾动作。
- `FIELD_TIME`：planner 内建站场动作，角色不应自己声明。
- `LEGACY_COMBO`：旧出招表动作。
- `DEFAULT_ACTION`：低价值兜底入口。

注意：

- E/Q 默认不需要额外带 `DAMAGE`，`SKILL_ACTION` / `ULTIMATE_ACTION` 已经有输出评分。
- 同一个 action 的 tag 是 set，重复标签不会重复加分。
- 切人评分不会累加同一角色所有 action；planner 只挑该角色当前最高分的 ready action 代表该角色参赛。
- tag 不控制入场链；是否继续尝试下一个 action 由 `EntryChainPolicy` 决定。

## EntryChainPolicy

`EntryChainPolicy` 控制一个 action 执行后，本次入场是否继续尝试下一个 action。
角色文件里推荐通过 `Planner.EntryChainPolicy` 使用；直接导入 `EntryChainPolicy`
也完全支持。

- `CONTINUE`：默认。成功后可继续；失败后也可尝试下一个 action。
- `STOP_ON_SUCCESS`：成功后停止；失败后可尝试下一个 action。
- `STOP`：执行后停止，无论成功或失败。

常见用法：

- 普通 Q/E 保持默认 `CONTINUE`，所以角色登场后可以自然尝试 Q 接 E。
- 需要“成功后像旧 perform return 一样立刻离场”时，使用 `STOP_ON_SUCCESS`。
- `planner_field_time`、旧出招表、等待类 action 使用 `STOP`。
- `ActionResult.tags` 不影响入场链。

## ActionSlot

`ActionSlot` 是协作匹配用的动作槽位，比 action name 更推荐。

常用槽位：

- `SKILL`：E。
- `ULTIMATE`：Q。
- `ARC`：弧盘。通常只用于 route/reservation 精确匹配；普通角色由
  `BaseChar.perform()` 统一按一次。
- `ENTRY_REACTION`：入场/环合反应，不是按键 action。
- `FIELD_TIME`：planner 内建站场。
- `LEGACY_COMBO`：旧出招表。
- `CUSTOM`：特殊动作。

协作和保留尽量写：

```python
FollowupStep.for_action(zero, ActionSlot.SKILL)
ActionReservation.for_action(nanally, ActionSlot.SKILL)
context.can_execute_action(self, slot=ActionSlot.SKILL)
```

不要依赖 `action_name`，除非同一个角色同一个 slot 下有多个特殊动作需要精确区分。

## BaseChar Helper

### click_ultimate_action

```python
self.click_ultimate_action(
    name=None,
    tags=None,
    reason="ultimate action available",
    can_execute=None,
    after_execute=None,
    chain_policy=Planner.EntryChainPolicy.CONTINUE,
)
```

含义：

- 自动设置 `slot=ActionSlot.ULTIMATE`。
- 默认 `tags={ActionTag.ULTIMATE_ACTION}`。
- 默认 `name=f"{角色名}_ultimate"`。
- `priority_ready` 自动使用 `self.ultimate_available()`。
- `execute` 调用 `self.click_ultimate()`。
- `after_execute(context, success)` 会在 Q 点击后执行。
- `slot=ULTIMATE` 会自动接入 reservation 检查。

### click_skill_action

```python
self.click_skill_action(
    name=None,
    tags=None,
    reason="skill action available",
    down_time=0.01,
    can_execute=None,
    after_execute=None,
    chain_policy=Planner.EntryChainPolicy.CONTINUE,
)
```

含义：

- 自动设置 `slot=ActionSlot.SKILL`。
- 默认 `tags={ActionTag.SKILL_ACTION}`。
- 默认 `name=f"{角色名}_skill"`。
- `priority_ready` 自动使用 `self.skill_available()`。
- `execute` 调用 `self.click_skill(down_time=down_time)`。
- `after_execute(context, success)` 会在 E 点击后执行。
- `slot=SKILL` 会自动接入 reservation 检查。

常见用法：

```python
self.click_skill_action(
    after_execute=lambda context, success: self.sleep(0.6)
    if success and self.ultimate_available()
    else None
)

self.click_ultimate_action(
    after_execute=lambda context, success: self.perform_in_ult(context)
    if success
    else None
)
```

`after_execute` 适合“按键后的角色内后处理”，例如失败补救、成功后短等待、
进入爆发段、发布一次性 planner request。E/Q helper 都把点击结果统一成
`success: bool` 传入 hook。

返回规则：

- 返回 `None`：保留原本点击成功判断。
- 返回 `True` / `False`：覆盖 action 最终成功状态。

### planner_action

```python
self.planner_action(
    tags={ActionTag.SKILL_ACTION},
    execute=self.some_action,
    name=None,
    slot=None,
    reason="",
    can_execute=None,
    priority_ready=None,
    chain_policy=Planner.EntryChainPolicy.CONTINUE,
)
```

参数：

- `tags`：必须能转成 set。推荐显式写 `{ActionTag.X}`。
- `execute`：接收 `CombatContext` 的函数。返回规则见 `ActionIntent`。
- `name`：高级精确匹配和日志名；普通动作可不传。
- `slot`：协作槽位；如果这个动作会被 route/reservation 匹配，应设置。
- `reason`：日志和切人原因。
- `can_execute`：额外 planner 层硬限制；slot reservation 会自动检查。
- `priority_ready`：切人评分 ready 判断。
- `chain_policy`：动作执行后是否继续本次入场。

## combat_intents

`combat_intents(context)` 只声明动作和入场诉求。

允许返回：

- `ActionIntent`
- `FieldClaim`
- `None`，会被 `self.intents(...)` 过滤

禁止在 `combat_intents()` 中调用：

- `context.request_route()`
- `context.reserve_actions()`
- `context.request_tags()`

原因：planner 会频繁读取 `combat_intents()` 来评分。如果评分扫描产生副作用，系统行为会变成暗箱。现在 planner 会 warning 并忽略从 `combat_intents()` 偷偷发布的请求。

## combat_policies

`combat_policies(context)` 用于随队伍生命周期长期生效的策略。planner reset 当前队伍时会调用。

适合：

- 常驻 reservation
- 队伍结构决定的长期规则

不适合：

- 某次 E 成功后才开启的窗口
- 某次 Q 成功后才发布的未来保护

示例：

```python
def combat_policies(self, context: CombatContext):
    context.reserve_actions(
        [ActionReservation.for_action(zero, ActionSlot.SKILL)],
        reason="reserve Zero skill for coordination",
        until=NEVER_EXPIRES,
    )
```

## FieldClaim

`FieldClaim` 表达“我应该被切进来”，不是动作。

```python
FieldClaim.normal("check records")
FieldClaim.high(reason="burst state active")
FieldClaim.critical(expected_entry=ExpectedEntry(slot=ActionSlot.ULTIMATE))
```

参数：

- `source`：可选。一般不传，planner 会自动把发布 claim 的角色作为来源。若第一个参数是字符串，会被当成 reason。
- `reason`：日志理由。
- `expected_entry`：可选。只有你明确要入场后强制某个动作时才传。

等级：

- `low`
- `normal`
- `high`
- `critical`

使用建议：

- 需要“之后抢回场”时用 FieldClaim。
- 不要用高分 action 假装站场诉求。
- 如果只是角色有 Q/E 可用，不需要额外 FieldClaim；action 本身会参与评分。

## SwitchInGuard

`switch_in_guard(context, from_char, has_intro)` 表达目标角色是否允许现在被切入。
guard 由“即将进场的角色”声明，避免当前角色替目标角色判断入场时机。

优先级规则很简单：只有 strict route 会跳过 `switch_in_guard`。其他普通切人、
entry reaction / retry intro 等目标切入，都应该尊重目标角色自己的入场保护。

默认：

```python
return SwitchInGuard.allow()
```

延迟切入：

```python
return SwitchInGuard.delay_until_ready(
    condition=lambda: self.animation_done,
    timeout=1.2,
    reason="wait target entry",
)
```

参数：

- `condition`：返回 True 表示可以切入。
- `timeout`：最多等待秒数，超时后不会卡死，会 warning 后继续切原目标。
- `reason`：日志。
- `poll_interval`：检查间隔。
- `while_waiting`：等待期间每轮执行的动作。默认 `None` 时，task 会使用当前角色
  `click_with_interval()`，避免角色原地发呆；若等待期间不能乱点，可传
  `while_waiting=lambda: None`。

## CombatContext 查询

`CombatContext` 中以下划线开头的字段是 planner 内部状态，角色代码不要读取或修改。
角色只通过本节公开方法查询状态或发布请求。

### can_execute_action

```python
context.can_execute_action(
    char,
    action_name="",
    tags=None,
    slot=ActionSlot.SKILL,
)
```

用于询问 planner 层是否允许某动作执行。主要受 reservation 影响。

推荐普通用法只传 `char` 和 `slot`。`action_name` / `tags` 是高级补充。

strict route 特例：如果当前 route 正在请求这个动作，即使有 reservation，也会允许执行。

### strict_route_wants_action

```python
context.strict_route_wants_action(self, slot=ActionSlot.SKILL)
```

用于角色自己的判断：某个动作平时不该放，但如果 strict route 点名就可以尝试。

### has_strict_route

```python
context.has_strict_route()
```

只查询当前是否存在正在锁定执行的 strict route。它不会把 reservation、
tag request、switch request 这类其他请求算进去。

### has_active_request

```python
context.has_active_request()
```

返回当前是否有未完成的协作请求或 strict route。常用于避免某些兜底动作干扰协作窗口。

## 协作请求

协作请求必须在动作执行成功后发布，或者在 `combat_policies()` 里发布长期策略。

所有 `request_*()` 和 `reserve_actions()` 都会返回 `RequestHandle | None`：

- 返回 `None`：没有实际发布请求，例如 steps/reservations/tags 为空。
- `handle.is_pending`：请求还没有出现 fulfilled、expired 或 closed 信号。
- `handle.is_fulfilled`：请求已成功满足。例如 route 步骤全部完成、switch 已切到目标。
- `handle.is_expired`：请求已过期或失效。例如 `until()` 返回 True、目标失效、旧 strict route 被新 route 覆盖。
- `handle.is_closed`：请求已经不再影响 planner。例如 strict route 已解除锁路、reservation 已释放、return-to-source 已结束。
- `handle.when`：不是状态，而是给另一个 request 的 `until=` 使用的条件集合。

`fulfilled`、`expired`、`closed` 是三个不同维度：

- `fulfilled` 表示 request 的目标已经满足。
- `expired` 表示 request 的生命周期已经结束或失效。
- `closed` 表示 request 已经离开 planner 调度/阻挡/锁路的影响面。
- route 这类请求可能先 fulfilled，之后窗口过期再 expired。
- `return_to_source=True` 的 route 会先 fulfilled，等发起者回场后才 closed。
- `when` 只是 predicate namespace，不是第三种状态。

常见组合：

```python
route = context.request_route(steps, reason="record route")

context.reserve_actions(
    holds,
    reason="hold until route succeeds",
    until=route.when.fulfilled,
)
```

这里的意思是：reservation 一直保留到 route 成功完成。如果要等 route 不再影响 planner，
使用 `until=route.when.closed`。如果要任一生命周期信号出现就释放，使用
`until=route.when.any`；如果只想 route 过期时释放，使用
`until=route.when.expired`。

### request_route

```python
route = context.request_route(
    steps,
    reason="record window",
    until=self.window_expired,
    return_to_source=False,
    on_finish=self.route_finished,
)
```

参数：

- `steps`：固定顺序路线。每个 `FollowupStep` 描述“哪个角色做哪个动作/入场反应”。
  空列表不会发布请求，返回 `None`。
- `reason`：日志和调试理由。建议写机制语义，例如 `"Hotori record route"`，
  不要只写 `"route"`。
- `until`：过期条件。为 `None` 时 route 不会因时间/机制条件自动过期；
  传 callable 时，返回 True 会让 route 以 `EXPIRED` 结束。
- `return_to_source`：route 成功完成后，是否提高发起者重新入场的优先级。
  适合“队友完成协作后回到发起者收尾”的流程。
- `on_finish`：route 首次出现 `FULFILLED` 或 `EXPIRED` 信号时调用一次；
  需要分别处理成功和过期时，保存返回的 `route` 后使用
  `route.on_fulfilled(...)` / `route.on_expired(...)`。

`request_route()` 返回 `RequestHandle`。普通角色通常只用它做两件事：

- `route.when.fulfilled`、`route.when.expired`、`route.when.closed` 或
  `route.when.any` 可作为其他 request 的 `until` 条件。
- `route.on_fulfilled(...)`、`route.on_expired(...)` 或 `route.on_finish(...)` 可注册无参回调。

摘要状态：

- `RequestStatus.PENDING`：请求还没有出现 fulfilled 或 expired 信号。
- `RequestStatus.FULFILLED`：请求已成功满足。
- `RequestStatus.EXPIRED`：请求已过期、目标失效，或被新的 strict route 替换。

如果同一个 handle 已经 fulfilled 后又 expired，`handle.status` 返回 `EXPIRED`，
但 `handle.is_fulfilled` 仍然保持 True。需要精确判断时优先使用
`handle.is_fulfilled` / `handle.is_expired` / `handle.is_closed`。`closed`
不属于 `RequestStatus`；它只表达 request 是否还影响 planner。

```python
route = context.request_route(steps, reason="record route")
route.on_fulfilled(self.route_done)
route.on_expired(self.route_expired)
```

`request_route()` 只负责路线，不负责动作保留。需要“路线 + 窗口保留”时，单独发布
`reserve_actions()`：

```python
route = context.request_route(
    steps,
    reason="record window",
    until=self.window_expired,
    on_finish=self.route_finished,
)

context.reserve_actions(
    holds,
    reason="record window reservations",
    until=self.window_expired,
    on_finish=self.window_reservations_finished,
)
```

区别：

- route fulfilled 只代表步骤完成，不代表机制窗口结束。
- 如果 reservation 应该随 route 完成释放，使用 `until=route.when.fulfilled`。
- 如果 reservation 应该随 route 过期释放，使用 `until=route.when.expired`。
- 如果 reservation 应该随 route 不再影响 planner 释放，使用 `until=route.when.closed`。
- 如果 reservation 应该持续到机制窗口结束，使用窗口自己的 `until` 条件。

### reserve_actions

```python
context.reserve_actions(
    [ActionReservation.for_action(zero, ActionSlot.SKILL)],
    reason="reserve Zero E",
    until=lambda: not self.has_reservation(),
    on_finish=self.reservation_finished,
)
```

参数：

- `reservations`：要保留的目标动作。每个 `ActionReservation` 描述目标角色和 slot。
  空列表不会发布请求，返回 `None`。
- `reason`：日志和调试理由。建议写清楚保留原因，例如 `"reserve Zero skill for record"`。
- `until`：必填生命周期。传 callable 表示返回 True 时释放；传 `Planner.NEVER_EXPIRES`
  表示持续到 planner reset，不会自动结束。
- `on_finish`：reservation 释放时调用的无参回调。`until=Planner.NEVER_EXPIRES` 时不能传，
  因为永久保留不会自动 finish。

reservation 不参与完成进度。它只回答“这个动作现在能不能执行”。

永久保留必须显式写：

```python
context.reserve_actions(
    [ActionReservation.for_action(zero, ActionSlot.SKILL)],
    reason="permanent Zero E reservation",
    until=Planner.NEVER_EXPIRES,
)
```

`Planner.NEVER_EXPIRES` 和直接导入的 `NEVER_EXPIRES` 是同一个对象；角色文件里推荐使用
`Planner.NEVER_EXPIRES` 来减少 import 数量。

`until=Planner.NEVER_EXPIRES` 不能搭配 `on_finish`。

### request_switch

```python
context.request_switch(
    zero,
    reason="switch to Zero after Hotori ultimate",
    until=self.switch_request_expired,
    on_finish=self.switch_request_finished,
)
```

`request_switch()` 是纯切人请求：

- 只请求下一次普通调度优先切给目标角色。
- 不要求目标角色执行指定动作。
- 不设置 `ExpectedEntry`。
- 不会打断当前角色的入场动作链。
- 不会让 `context.has_active_request()` 变成 True。
- 切到目标角色后，由目标自己的 `combat_intents()` 正常决定动作。

优先级：

- strict route、entry reaction、环合反应高于 `request_switch()`。
- 如果这些硬调度先抢走切人，switch request 不会立刻丢弃，会保留到后续普通调度。
- 请求在实际切到目标、目标已经是当前角色、目标离队、或 `until()` 返回 True 时结束。

参数：

- `target`：希望切入的目标角色。为 `None` 时不会发布请求，返回 `None`。
- `reason`：日志和调试理由。
- `until`：过期条件。为 `None` 时不会因时间/机制条件自动过期；返回 True 时
  request 以 `EXPIRED` 结束。
- `on_finish`：请求首次出现 `FULFILLED` 或 `EXPIRED` 信号时调用一次。切到目标
  或目标已在场是 `FULFILLED`；目标失效或 `until()` 触发是 `EXPIRED`。

### request_tags

`request_tags()` 是高级逃生口，不是普通协作角色的推荐入口。

优先使用：

- 需要固定顺序时：`request_route(...)`
- 需要固定顺序且窗口内保留动作时：组合 `request_route(...)` 和 `reserve_actions(...)`
- 只需要保留某些动作时：`reserve_actions(...)`

只有需求真的是“任意队友完成某类通用动作”时才使用 `request_tags()`。
不要用 tag 表达某个角色专属机制；专属机制应通过 `FollowupStep`、`ActionSlot`
和 `ActionReservation` 明确建模。

```python
context.request_tags(
    {ActionTag.SUPPORT},
    count=1,
    reason="needs support",
    until=self.request_expired,
    avoid_source=True,
    return_to_source=True,
    on_finish=self.request_finished,
)
```

参数：

- `tags`：需要的动作标签集合。目标动作只要命中任一 tag 就可以计数。
  空集合不会发布请求，返回 `None`。
- `count`：需要完成几名角色/几次动作。小于等于 0 时不会发布请求。
- `reason`：日志和调试理由。
- `until`：过期条件。为 `None` 时不会因时间/机制条件自动过期；返回 True 时
  request 以 `EXPIRED` 结束。
- `avoid_source`：未完成前是否避开发起者。默认 True，避免发起者自己立刻满足请求。
- `return_to_source`：成功完成后，是否提高发起者重新入场优先级。
- `on_finish`：请求首次出现 `FULFILLED` 或 `EXPIRED` 信号时调用一次；不需要参数。

## FollowupStep

固定路线的一步。

```python
FollowupStep.for_action(
    target,
    ActionSlot.SKILL,
    reason="Zero E",
    optional=False,
    required_tags=None,
    action_names=None,
)
```

参数：

- `target`：目标角色对象。
- `slot`：目标动作槽位。
- `reason`：日志。
- `optional`：True 时该步骤不可用或失败可以跳过。
- `required_tags`：额外要求 action 带有任一指定 tag。
- `action_names`：高级精确匹配。只有 slot 不够表达时使用。

入场/环合反应：

```python
FollowupStep.for_entry_reaction(nanally, reason="Nanally entry")
```

这一步不是按键 action。planner 会等实际切人触发 entry reaction 后推进。

## ActionReservation

保留某角色的某些动作槽位。

```python
ActionReservation.for_action(nanally, ActionSlot.SKILL)
ActionReservation.for_slots(zero, [ActionSlot.SKILL, ActionSlot.ULTIMATE])
```

reservation 只限制 planner 层动作执行。角色内部长循环中如果要动态判断，也要调用：

```python
context.can_execute_action(self, slot=ActionSlot.SKILL)
```

## 调度行为

切人评分：

- 每个角色只取最高分且 `priority_ready=True` 的 action 参与评分。
- 该 action 只用于判断“是否值得切这个角色出来”。
- 普通切人不会把最高分 action 强制成入场首动。
- strict route / entry reaction 会设置 expected entry，强制目标动作。
- 普通评分切人日志会带 `score_breakdown`，用于解释分数来源，例如：

```text
planner switch A -> B, priority 115, reason skill action available,
score_breakdown [action_tags(skill_action)=+75, role:sub_dps=+40 => 115]
```

常见分数组件：

- `action_tags(...)`：当前用于评分的最高分 action 的 tag 分。
- `field_time_tags(...)`：没有 ready action 时，内建 field time 的 tag 分。
- `field_claim:<level>`：`FieldClaim` 入场诉求分。
- `request_wants_action`：active request 初步匹配该 action。
- `fulfill_request`：该 action 可完成 active request。
- `return_to_source`：协作完成后回到发起者。
- `role:<field_preference>`：角色站场偏好分。
- `switch_cooldown`：非 intro 切人冷却惩罚。

进场执行：

- planner 每个决策/动作 loop 会缓存一次 `combat_intents(context)` 结果，避免同一轮重复扫描。
- strict route action 优先。
- active tag request 会在声明顺序中优先挑能完成请求的 action。
- 普通路径按声明顺序挑第一个 allowed action。
- 同一次入场不会重复执行同一个 action identity。
- 是否继续尝试下一 action 由 `EntryChainPolicy` 决定。

停止本次入场的常见情况：

- 动作失败且不能继续尝试其他动作。
- 动作发布了协作请求。
- action 的 `chain_policy` 为 `STOP` 或成功后的 `STOP_ON_SUCCESS`。
- 当前还有 active request 需要重新调度。
- strict route 下一步不在当前角色。

field time：

- planner 内建兜底。
- 只有所有 action 都失败或没有动作，并且 `max_field_time` 还有剩余时执行。
- 角色不应自己声明 `FIELD_TIME` action。

## Hotori 示例

Hotori 在角色内用 `HotoriRecordPlan` 保留三组 reservation：

- `record_window_holds`：E 开窗期间临时 hold 的动作，例如 Zero + Nanally 队伍里的 Nanally E。
- `after_ultimate_reservations`：Q 成功后保护下一次 E 路线会用到的队友技能。
- `combat_reservations`：Zero + Nanally 体系下常驻保护 Zero E，避免还没到 Hotori 记录路线就被提前消耗。

队伍加载后：

- `combat_policies()` 使用 `until=Planner.NEVER_EXPIRES` 发布 `combat_reservations` 常驻保护。

E 成功后：

- `start_records()`。
- `context.request_route(...)` 发布队友记录路线。
- `record_window_holds` 保留持续到窗口结束，即使 route 已完成也不会提前释放。

Q 成功后：

- `records_ready = False`。
- `start_reservation()`。
- `context.reserve_actions(after_ultimate_reservations, until=lambda: not self.has_reservation())`，保护下一次 E 开窗需要的队友 E。

Zero + Nanally 同队时：

- route 使用 Zero E 接 Nanally entry。
- Nanally E 不作为记录步骤。
- Nanally E 在记录窗口内会被 hold，避免覆盖记录。

缺少 Zero 或 Nanally 时：

- fallback 为队友 E 记录路线。
- reserve 对应队友的 `SKILL`。

不要把 Hotori 的 reservation 分组压平，否则会改变资源保护策略。

## 常见误区

- `combat_intents()` 返回顺序不是切人评分顺序，但普通进场执行会参考声明顺序。
- `priority_ready=False` 只影响切人评分，不等于动作永远不能执行。
- `can_execute=False` 是硬限制，动作不会执行。
- `execute` 必须返回严格的 `True` 才算成功。
- `ActionResult.tags` 通常自动继承 action tags；手写 `ActionResult` 时要自己负责 tags。
- `ActionResult.tags` 不控制入场链。
- Q/E 不需要额外带 `DAMAGE`。
- `reserve_actions()` 必须显式写 `until=...`；永久保留写 `until=Planner.NEVER_EXPIRES`。
- `request_route()` 不接受 holds；需要持续到窗口结束时，单独发布 `reserve_actions()` 并绑定窗口生命周期。
- `FieldClaim` 是入场诉求，不是动作。
- `ActionReservation` 是保留动作，不是 route step。
