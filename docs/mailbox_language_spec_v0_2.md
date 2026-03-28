# Mailbox Language Spec v0.2

状态：**收敛版规范**。本版不再继续发散概念，目标是直接支持 parser、checker 和 runtime/IR 实现。

本规范冻结以下主张：

- `mailbox` 是**多协议入口**
- `thread` 是**单协议会话**，并绑定完整 `(protocol, version)`
- `PlainText` 是**内建协议**，但不是全局兜底
- 未带协议的外部 ingress 只能走 `mailbox.default`
- v0 中 `default` 只允许指向 `PlainText/*`
- 显式指定的 typed `protocol` **绝不允许**失败后回退到 `PlainText`
- `PlainText -> typed protocol` 只能通过显式桥接，不允许线程内静默切换

---

## 1. 收敛原则

这版规范刻意做了减法，只保留一套能够稳定实现的最小主干。

### 1.1 保留的核心能力

1. 声明版本化 `protocol`
2. 声明多协议 `mailbox`
3. 创建新 thread：`send to <mailbox> using <Protocol.Message>`
4. 向已有 thread 继续发送：`send to <thread> using <Message>`
5. 保留 `PlainText` 快捷入口：`send text to <mailbox> ...`
6. 保留跨协议桥接：`spawn ... from <thread>` 与 `handoff`

### 1.2 明确砍掉的能力

为避免 v0 语义松散，本版**不引入**：

- 基于 payload 的隐式 protocol 推断
- 任意 protocol 作为 mailbox 默认协议
- 线程内静默切换 protocol
- pub/sub、stream、request/reply 等通信模式语法
- capability / auth / effect 的完整语言层声明
- `upgrade` 语法（同协议族版本迁移留待下一版；v0 统一用 `spawn + handoff` 表达）

---

## 2. 冻结的设计决策

## 2.1 mailbox 多态，thread 单态

一个 `mailbox` 可以接多个 protocol；一个 `thread` 在其生命周期内只能绑定一个主协议。

```text
mailbox support_mb {
  accepts [PlainText/v1, Orders/v2, Support/v1];
  default PlainText/v1;
}
```

```text
thread #T1 : thread<PlainText/v1>
thread #T2 : thread<Orders/v2>
```

同一个 thread 不允许在内部从 `PlainText/v1` 静默变成 `Orders/v2`。

## 2.2 PlainText 是可声明默认入口，不是全局兜底

`PlainText` 必须出现在 `mailbox.accepts` 中才可被接收。没有声明就不能接收无格式输入。

```text
mailbox orders_mb {
  accepts [Orders/v2];
}
```

上面这个 mailbox 对 `PlainText` 是关闭的。

## 2.3 default 只服务“无协议 ingress”，不服务 typed send 回退

`default` 的作用是接纳**没有 protocol 标签**的外部输入；它**不是** typed send 的回退机制。

因此：

- 外部消息没有 protocol 标记时，runtime 可以使用 `mailbox.default`
- 语言内显式 typed `send` 如果失败，必须报错，不能自动降级为 `PlainText`

## 2.4 语言层不提供“泛化的无协议 send”

为了避免语义模糊，v0 源语言只保留两类入口：

1. **typed send**：显式写出 `Protocol.Message`
2. **text send**：使用 `send text` 语法糖显式表示进入 `PlainText`

也就是说，v0 **不支持**这样模糊的写法：

```text
send to support_mb { body: "hello" };
```

如果要发自然语言，应写成：

```text
send text to support_mb "hello";
```

这样 `default` 仍然保留给 adapter / gateway / external ingress 使用，但不会污染语言层语义。

---

## 3. 核心模型

## 3.1 ProtocolRef

`ProtocolRef` 是 protocol 名称与版本的组合。

```ebnf
ProtocolRef ::= Ident "/" Version
Version     ::= Ident | Number | StringLit
```

v0 规范要求：**凡是出现在声明和 mailbox 目标 send 中的 protocol，都必须显式带版本。**

合法例子：

```text
PlainText/v1
Orders/v2
Support/v2026_03
```

不建议在 v0 中省略版本。

## 3.2 MessageRef

```ebnf
MessageRef ::= ProtocolRef "." Ident
             | Ident
```

约束：

- **发往 mailbox 的新 thread**：`MessageRef` 必须是全限定 `ProtocolRef.Message`
- **发往已有 thread**：可使用非限定消息名 `Message`

例子：

```text
Orders/v2.QuoteReq
Orders/v2.Cancel
Approve
```

## 3.3 ThreadHandle

thread handle 的静态类型记为：

```text
thread<Orders/v2>
thread<PlainText/v1>
```

语义上，thread handle 至少绑定：

- `thread_id`
- `protocol`
- `version`
- 当前 `state`

## 3.4 Built-in PlainText

`PlainText` 是内建 protocol，但语义上依然是一个 protocol，而不是“无约束空通道”。

v0 内建如下：

```text
builtin protocol PlainText/v1 {
  state Open;
  start Open;

  message Text {
    subject?: String;
    body: String;
    attachments?: [Attachment];
    sender?: Principal;
    auth?: AuthContext;
  }

  on Text from Open -> Open;
}
```

重点：

- `PlainText` 也有明确消息类型 `Text`
- 只是业务语义不结构化，不代表 envelope 不结构化

---

## 4. Canonical 语法

## 4.1 Protocol declaration

```text
protocol Orders/v2 {
  state Init;
  state AwaitDecision;
  state Done;

  start Init;

  message QuoteReq {
    order_id: String;
    items: [OrderItem];
  }

  message Approve {
    order_id: String;
  }

  message Cancel {
    order_id: String;
    reason?: String;
  }

  on QuoteReq from Init -> AwaitDecision;
  on Approve  from AwaitDecision -> Done;
  on Cancel   from AwaitDecision -> Done;
}
```

v0 中 `protocol` 只包含：

- `state`
- `start`
- `message`
- `on ... from ... -> ...`

不包含 guard、capability、effect、timeout 等更复杂构件。

## 4.2 Mailbox declaration

canonical form：

```text
mailbox support_mb {
  accepts [PlainText/v1, Orders/v2];
  default PlainText/v1;
}

mailbox orders_mb {
  accepts [Orders/v2];
}
```

约束：

1. `accepts` 必填
2. `accepts` 中元素必须唯一
3. mailbox 可以接多个 protocol
4. v0 中一个 mailbox **至多接受一个** `PlainText/*` 版本
5. `default` 可选
6. 若声明 `default`：
   - 它必须属于 `accepts`
   - 它必须属于 `PlainText/*`

### 4.2.1 Mailbox shorthand

保留轻量语法糖：

```text
mailbox support_mb : PlainText/v1 | Orders/v2;
mailbox orders_mb  : Orders/v2;
```

展开规则：

```text
mailbox support_mb : PlainText/v1 | Orders/v2;
```

等价于：

```text
mailbox support_mb {
  accepts [PlainText/v1, Orders/v2];
  default PlainText/v1;
}
```

```text
mailbox orders_mb : Orders/v2;
```

等价于：

```text
mailbox orders_mb {
  accepts [Orders/v2];
}
```

v0 对 shorthand 的限制：

- 单协议形式总是允许
- 多协议 shorthand 只允许 `PlainText/*` 位于首位，且自动成为 `default`
- 其他复杂场景必须使用 canonical body form

---

## 4.3 创建新 thread：send to mailbox

创建新 thread 的 canonical 语法：

```text
let t = send to orders_mb using Orders/v2.QuoteReq {
  order_id: "123";
  items: cart_items;
};
```

语义：

- 目标是 `mailbox`
- 使用显式 protocol 和 message 创建新 thread
- 返回值类型为 `thread<Orders/v2>`
- runtime 在 `Orders/v2.start` 状态上应用入口消息

v0 中，**发往 mailbox 的新 thread 必须使用全限定消息名**。

下面这种写法非法：

```text
send to orders_mb using QuoteReq { ... };
```

因为 mailbox 级别存在多 protocol 可能性，不允许靠 message 名做推断。

## 4.4 向已有 thread 继续发送

```text
send to t using Approve {
  order_id: "123";
};
```

语义：

- `t` 已经绑定 `Orders/v2`
- `Approve` 解析为 `Orders/v2.Approve`
- checker/runtime 使用 `t.state` 校验该消息是否可接收

也允许更显式的写法：

```text
send to t using Orders/v2.Approve {
  order_id: "123";
};
```

但其语义仅用于确认，不允许覆盖 thread 绑定。

若写成：

```text
send to t using Support/v1.Reply { ... };
```

则必须直接报错。

### 4.4.1 返回值

- `send to <mailbox> ...`：返回新 thread handle
- `send to <thread> ...`：返回 `unit`

---

## 4.5 PlainText 快捷入口：send text

为了给自然语言/无格式输入保留明确入口，v0 保留：

```text
let text_t = send text to support_mb "请帮我取消订单 123";
```

它等价于：

```text
let text_t = send to support_mb using PlainText/v1.Text {
  body: "请帮我取消订单 123";
};
```

也允许 block form：

```text
let text_t = send text to support_mb {
  subject: "取消订单";
  body: "请帮我取消订单 123";
  attachments: [invoice_pdf];
};
```

约束：

1. `send text` 的目标必须是 mailbox，而不是 thread
2. 目标 mailbox 必须接受某个唯一的 `PlainText/*` 版本
3. 若 mailbox 不接受 `PlainText/*`，则报错
4. `send text` 不允许绕过 `accepts` 检查

如果未来一个 mailbox 同时接受多个 PlainText 版本，则 v0 视为不合法配置；需等下一版再讨论版本选择规则。

---

## 4.6 跨协议桥接：spawn

`PlainText -> Orders` 这类跨协议迁移，必须通过创建新的 typed thread 来表达。

```text
let order_t = spawn to orders_mb using Orders/v2.Cancel {
  order_id: "123";
  reason: "parsed from text thread";
} from text_t;
```

语义：

- 在 `orders_mb` 上创建一个新的 `thread<Orders/v2>`
- 记录其来源于 `text_t`
- `text_t` 自身仍然保持 `thread<PlainText/v1>`
- 不发生线程内 protocol 切换

### 4.6.1 返回值

`spawn` 返回新创建的 thread handle。

---

## 4.7 处理权转移：handoff

```text
handoff text_t -> order_t;
```

语义：

- 记录“后续处理主路径从 `text_t` 转交给 `order_t`”
- 这是控制平面/审计语义
- 不改变任一 thread 的 protocol 或 state
- 不隐式关闭原 thread

如果需要关闭、完成、归档原 thread，必须继续通过正常 protocol message 表达，而不是让 `handoff` 隐式承担业务语义。

---

## 5. EBNF（v0 冻结草案）

```ebnf
Program           ::= { Decl | Stmt }

Decl              ::= ProtocolDecl | MailboxDecl

ProtocolDecl      ::= ["builtin"] "protocol" ProtocolRef "{" { ProtocolItem } "}"
ProtocolItem      ::= StateDecl | StartDecl | MessageDecl | TransitionDecl
StateDecl         ::= "state" Ident ";"
StartDecl         ::= "start" Ident ";"
MessageDecl       ::= "message" Ident "{" { FieldDecl } "}"
FieldDecl         ::= Ident ["?"] ":" Type ";"
TransitionDecl    ::= "on" Ident "from" Ident "->" Ident ";"

MailboxDecl       ::= "mailbox" Ident MailboxBody
                    | "mailbox" Ident ":" MailboxShorthand ";"
MailboxBody       ::= "{" "accepts" "[" ProtocolRef { "," ProtocolRef } "]" ";"
                           ["default" ProtocolRef ";"] "}"
MailboxShorthand  ::= ProtocolRef
                    | PlainTextRef "|" ProtocolRef { "|" ProtocolRef }
PlainTextRef      ::= "PlainText" "/" Version

Stmt              ::= LetStmt | SendStmt | SendTextStmt | SpawnStmt | HandoffStmt
LetStmt           ::= "let" Ident [":" Type] "=" Expr ";"
Expr              ::= SendExpr | SendTextExpr | SpawnExpr | Ident | Literal

SendStmt          ::= "send" "to" Dest "using" MessageRef PayloadBlock ";"
SendExpr          ::= "send" "to" Dest "using" MessageRef PayloadBlock
SendTextStmt      ::= "send" "text" "to" MailboxRef (StringLit | PayloadBlock) ";"
SendTextExpr      ::= "send" "text" "to" MailboxRef (StringLit | PayloadBlock)
SpawnStmt         ::= "spawn" "to" MailboxRef "using" MessageRef PayloadBlock "from" ThreadRef ";"
SpawnExpr         ::= "spawn" "to" MailboxRef "using" MessageRef PayloadBlock "from" ThreadRef
HandoffStmt       ::= "handoff" ThreadRef "->" ThreadRef ";"

Dest              ::= MailboxRef | ThreadRef
MailboxRef        ::= Ident
ThreadRef         ::= Ident | "#" Ident
PayloadBlock      ::= "{" { PayloadField } "}"
PayloadField      ::= Ident ":" Expr ";"

Type              ::= Ident
                    | "thread" "<" ProtocolRef ">"
                    | "[" Type "]"
```

---

## 6. 静态语义

## 6.1 声明阶段校验

### Rule D1: protocol 定义合法性

- 一个 `protocol` 内的 `state` 名必须唯一
- `message` 名必须唯一
- `start` 必须引用已声明 state
- 每条 `on Msg from S1 -> S2` 中：
  - `Msg` 必须已声明
  - `S1`、`S2` 必须已声明

### Rule D2: mailbox 定义合法性

- `accepts` 至少有一个元素
- `accepts` 中不允许重复 `ProtocolRef`
- 至多一个 `PlainText/*`
- 若声明 `default`：
  - `default ∈ accepts`
  - `default` 必须属于 `PlainText/*`

### Rule D3: shorthand 合法性

- `mailbox m : P;` 合法
- `mailbox m : PlainText/v1 | P1 | P2;` 合法
- `mailbox m : Orders/v2 | Support/v1;` 不合法；请改用 canonical body form

## 6.2 发送语句解析规则

### Rule S1: send to mailbox

```text
send to M using P.Msg { ... }
```

要求：

1. `M` 是 mailbox
2. `P ∈ accepts(M)`
3. `Msg` 属于 `P`
4. payload 满足 `P.Msg` schema
5. `Msg` 必须能从 `start(P)` 发出

成功后：

- 创建新 thread `T`
- 绑定 `T.protocol = P`
- 绑定 `T.state = transition(start(P), Msg)`
- 返回 `thread<P>`

### Rule S2: send to existing thread（省略 protocol）

```text
send to T using Msg { ... }
```

要求：

1. `T` 是 thread handle
2. `Msg` 在 `T.protocol` 中存在
3. payload 满足 `T.protocol.Msg` schema
4. `Msg` 在 `T.state` 下合法

成功后：

- 更新 `T.state`
- 返回 `unit`

### Rule S3: send to existing thread（显式 protocol）

```text
send to T using P.Msg { ... }
```

要求：

1. `P == T.protocol`
2. 其余规则同 S2

若 `P != T.protocol`，必须直接报错。

### Rule S4: send text to mailbox

```text
send text to M ...
```

要求：

1. `M` 是 mailbox
2. `M` 接受且只接受一个 `PlainText/*` 版本
3. payload 满足对应 `PlainText/*.Text` schema

成功后等价于：

```text
send to M using <that PlainText version>.Text { ... }
```

### Rule S5: spawn

```text
spawn to M using P.Msg { ... } from T0
```

要求：

1. `M` 是 mailbox
2. `T0` 是已有 thread
3. `P ∈ accepts(M)`
4. `Msg` 属于 `P`
5. payload 合法
6. `Msg` 能从 `start(P)` 发出

成功后：

- 创建新 thread `T1 : thread<P>`
- 记录 `T1.parent_thread = T0`
- `T0` 本身不发生 protocol 变化
- 返回 `T1`

### Rule S6: handoff

```text
handoff T0 -> T1
```

要求：

- `T0` 与 `T1` 均存在

成功后：

- 记录一条 handoff 关系
- 不改变类型和状态

## 6.3 不可违反的不变量

以下规则属于语言硬约束，不建议做成配置项：

1. **一个 thread 只能绑定一个主协议**
2. **显式 typed protocol 绝不回退到 PlainText**
3. **PlainText 只有在 mailbox 明确声明时才可接收**
4. **跨协议迁移只能通过 spawn（以及可选 handoff）表达**
5. **发往 mailbox 的 typed send 必须全限定**
6. **语言层不提供泛化的无协议 send**

---

## 7. Runtime 语义

## 7.1 新 thread 的建立

当 runtime 执行：

```text
send to M using P.Msg { payload }
```

它必须：

1. 校验 `P ∈ accepts(M)`
2. 校验 `Msg ∈ P.messages`
3. 校验 `payload` schema
4. 创建 thread，初始状态为 `start(P)`
5. 应用 `Msg`
6. 将 thread 状态推进到目标状态

若任何一步失败，则整个操作失败，且不得留下半创建 thread。

## 7.2 既有 thread 的推进

向已有 thread 发消息时：

1. runtime 从 handle 读取 `thread_id` 与绑定的 `protocol`
2. 校验消息属于该 protocol
3. 根据当前 state 检查 transition
4. 成功则推进状态，失败则原子拒绝

## 7.3 外部 ingress 的默认协议规则

这一条不是语言语法，而是 mailbox runtime 行为：

当外部 adapter / gateway 投递一个**没有 protocol 标签**的输入到 mailbox 时：

- 若 mailbox 定义了 `default`，则使用该 `default`
- 若 mailbox 未定义 `default`，则拒绝

v0 由于 `default` 必须是 `PlainText/*`，因此“无协议 ingress”在语义上等价于进入 `PlainText`。

## 7.4 spawn / handoff

`spawn` 是数据平面动作：创建新 thread。  
`handoff` 是控制平面动作：记录处理权转移关系。

二者可以独立发生，也可以组合使用：

```text
let order_t = spawn to orders_mb using Orders/v2.Cancel {
  order_id: "123";
  reason: "parsed from text thread";
} from text_t;

handoff text_t -> order_t;
```

## 7.5 拒绝条件

建议 runtime 至少提供以下错误类别：

- `E_MAILBOX_PROTOCOL_NOT_ACCEPTED`
- `E_MAILBOX_TEXT_NOT_ACCEPTED`
- `E_MESSAGE_UNKNOWN`
- `E_THREAD_PROTOCOL_MISMATCH`
- `E_STATE_TRANSITION_INVALID`
- `E_PAYLOAD_SCHEMA_INVALID`
- `E_MAILBOX_DEFAULT_INVALID`
- `E_PROTOCOLLESS_INGRESS_REJECTED`

---

## 8. IR Lowering

v0 建议将 surface syntax lowering 到一个统一 envelope/event 级 IR。

## 8.1 MessageEnvelope

```text
MessageEnvelope {
  op: "send" | "spawn";
  target_kind: "mailbox" | "thread";

  mailbox?: MailboxId;
  thread_id?: ThreadId;

  protocol: ProtocolName;
  version: Version;
  msg_type: MessageName;
  payload: Value;

  parent_thread_id?: ThreadId;   # only for spawn
}
```

## 8.2 HandoffEvent

```text
HandoffEvent {
  op: "handoff";
  from_thread_id: ThreadId;
  to_thread_id: ThreadId;
}
```

## 8.3 Lowering rules

### A. New typed thread

```text
let t = send to orders_mb using Orders/v2.QuoteReq {
  order_id: "123";
  items: cart_items;
};
```

lower 为：

```text
MessageEnvelope {
  op: "send";
  target_kind: "mailbox";
  mailbox: orders_mb;
  protocol: "Orders";
  version: "v2";
  msg_type: "QuoteReq";
  payload: {
    order_id: "123",
    items: cart_items,
  };
}
```

### B. Continue existing thread

```text
send to t using Approve {
  order_id: "123";
};
```

lower 为：

```text
MessageEnvelope {
  op: "send";
  target_kind: "thread";
  thread_id: t.thread_id;
  protocol: t.protocol.name;
  version: t.protocol.version;
  msg_type: "Approve";
  payload: {
    order_id: "123",
  };
}
```

### C. send text

```text
send text to support_mb "请帮我取消订单 123";
```

lower 为：

```text
MessageEnvelope {
  op: "send";
  target_kind: "mailbox";
  mailbox: support_mb;
  protocol: "PlainText";
  version: "v1";
  msg_type: "Text";
  payload: {
    body: "请帮我取消订单 123",
  };
}
```

### D. spawn

```text
spawn to orders_mb using Orders/v2.Cancel {
  order_id: "123";
  reason: "parsed from text thread";
} from text_t;
```

lower 为：

```text
MessageEnvelope {
  op: "spawn";
  target_kind: "mailbox";
  mailbox: orders_mb;
  protocol: "Orders";
  version: "v2";
  msg_type: "Cancel";
  payload: {
    order_id: "123",
    reason: "parsed from text thread",
  };
  parent_thread_id: text_t.thread_id;
}
```

---

## 9. 示例

## 9.1 开放入口 + typed flow

```text
protocol Orders/v2 {
  state Init;
  state AwaitDecision;
  state Done;

  start Init;

  message QuoteReq {
    order_id: String;
    items: [OrderItem];
  }

  message Cancel {
    order_id: String;
    reason?: String;
  }

  on QuoteReq from Init -> AwaitDecision;
  on Cancel   from AwaitDecision -> Done;
}

mailbox support_mb : PlainText/v1 | Orders/v2;
mailbox orders_mb  : Orders/v2;
```

### 入口 1：自然语言

```text
let text_t = send text to support_mb "请帮我取消订单 123";
```

### 入口 2：直接 typed

```text
let order_t = send to orders_mb using Orders/v2.QuoteReq {
  order_id: "123";
  items: cart_items;
};
```

## 9.2 PlainText -> Orders 桥接

```text
let text_t = send text to support_mb "请帮我取消订单 123";

let order_t = spawn to orders_mb using Orders/v2.Cancel {
  order_id: "123";
  reason: "parsed from text thread";
} from text_t;

handoff text_t -> order_t;
```

## 9.3 Orders-only mailbox 拒绝无格式 mail

```text
mailbox orders_mb : Orders/v2;

send text to orders_mb "请帮我取消订单 123";
# error: E_MAILBOX_TEXT_NOT_ACCEPTED
```

## 9.4 线程内 protocol mismatch

```text
let order_t = send to orders_mb using Orders/v2.QuoteReq {
  order_id: "123";
  items: cart_items;
};

send to order_t using Support/v1.Reply {
  body: "hello";
};
# error: E_THREAD_PROTOCOL_MISMATCH
```

## 9.5 mailbox 目标不允许省略 protocol

```text
send to orders_mb using QuoteReq {
  order_id: "123";
  items: cart_items;
};
# error: mailbox-target send must use ProtocolRef.Message
```

---

## 10. 为什么这样收敛

这版的关键不是“功能最多”，而是“边界最稳”。

### 10.1 把不确定入口与确定流程分开

- `PlainText` 承接人类自然语言、未知需求、例外输入
- typed protocol 承接稳定流程、自动化、可审计执行

### 10.2 不把 default 变成类型系统后门

如果 typed send 失败后能回退到 `PlainText`，那类型边界会立刻失效。  
所以 `default` 只给无协议 ingress 用，不给 typed send 容错用。

### 10.3 不在线程内切协议

线程内切协议会让 replay、审计、debug、状态机验证全部变复杂。  
因此跨协议只允许 `spawn`，必要时再加 `handoff`。

### 10.4 不允许 mailbox 目标用 message 名猜 protocol

多协议 mailbox 上，`QuoteReq` 本身不足以决定要进哪个 protocol。  
所以 mailbox 目标 send 必须全限定。

---

## 11. v0 实现建议

建议实现顺序：

1. **Parser**
   - 支持 `protocol`
   - 支持 `mailbox` canonical form
   - 支持 shorthand
   - 支持 `send / send text / spawn / handoff`

2. **Declaration checker**
   - 建 protocol table
   - 建 mailbox table
   - 校验 `accepts/default`
   - 校验 transitions

3. **Type checker**
   - 推导 `thread<P>`
   - 校验 mailbox-target 和 thread-target send
   - 校验 `spawn` / `handoff`

4. **Lowering**
   - 统一降到 `MessageEnvelope` / `HandoffEvent`

5. **Runtime**
   - 实现 mailbox acceptance
   - 实现 state transition
   - 实现 external ingress default 规则
   - 实现 spawn relation / handoff relation

---

## 12. 下一版再讨论的内容

以下内容被明确推迟到 v0 之后：

- capability / auth 的语言层声明
- request/reply、stream、publish 等通信模式
- 更丰富的 schema/type system
- protocol guard / effect / timeout
- 同协议族版本迁移的 `upgrade` 语法与 state mapping
- 公共控制消息（Ack、Error、Timeout）是否纳入语言表层

---

## 13. 结论

v0 的最终定稿可以概括为一句话：

> `mailbox` 是多协议入口，`thread` 是单协议会话；`PlainText` 是可声明默认入口，但不是 typed protocol 的回退；跨协议只能通过显式派生新 thread 来完成。

这套收敛后的设计兼顾了：

- 开放入口
- 强约束流程
- 明确类型边界
- 可实现的 parser/checker/runtime 路径
- 稳定的 IR lowering

