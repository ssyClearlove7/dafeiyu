-- events：实际获得的消息；account/room/actor为稳定身份，at为事件UTC秒。
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  source TEXT NOT NULL UNIQUE, -- 原消息身份 SHA256 摘要，包含账号、会话和组件消息编号
  account TEXT NOT NULL, -- 机器人账号的真实稳定微信 ID
  room TEXT NOT NULL, -- 原会话稳定 ID，作为记忆与操作权限边界
  actor TEXT NOT NULL, -- 真实发送者或发起者稳定微信 ID
  direction TEXT NOT NULL, -- 消息方向字典：incoming、outgoing、tool、observation
  kind TEXT NOT NULL, -- 业务种类字典：消息媒体类型、记忆知识类型或人格变更类型
  body TEXT NOT NULL, -- 消息原文、文字理解结果或记忆正文
  payload TEXT NOT NULL, -- 来源元信息 JSON；origins 为派生内容的原事件编号数组
  at REAL NOT NULL, -- 实际事件或请求发起时间，UTC Unix 秒
  created REAL NOT NULL, -- 本记录创建时间，UTC Unix 秒
  done INTEGER NOT NULL DEFAULT 0 -- 原文整理进度：0 待整理、1 已整理；不删除原文
);
CREATE INDEX IF NOT EXISTS event_scope ON events(account,room,id);
-- notes：按来源版本保存知识；origin为来源事件ID数组，room限定可见会话。
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  account TEXT NOT NULL, -- 机器人账号的真实稳定微信 ID
  room TEXT NOT NULL, -- 原会话稳定 ID，作为记忆与操作权限边界
  actor TEXT NOT NULL, -- 真实发送者或发起者稳定微信 ID
  topic TEXT NOT NULL, -- 同一会话中的记忆主题名称
  body TEXT NOT NULL, -- 消息原文、文字理解结果或记忆正文
  origin TEXT NOT NULL, -- 来源事件编号 JSON 数组，用于追溯、隔离与删除同步
  previous INTEGER, -- 同主题上一条 notes.id，可为空
  kind TEXT NOT NULL DEFAULT 'memory', -- 业务种类字典：消息媒体类型、记忆知识类型或人格变更类型
  created REAL NOT NULL -- 本记录创建时间，UTC Unix 秒
);
CREATE INDEX IF NOT EXISTS note_scope ON notes(account,room,id);
-- lookup：全文索引；terms 为分词，kind 区分索引目标，ref 关联原记录编号。
CREATE VIRTUAL TABLE IF NOT EXISTS lookup USING fts5(terms,kind UNINDEXED,ref UNINDEXED);
-- tombstones：只保存原消息身份摘要，防止恢复或补采重新引入已删除正文。
CREATE TABLE IF NOT EXISTS tombstones (
  source TEXT PRIMARY KEY, -- 原消息身份 SHA256 摘要，包含账号、会话和组件消息编号
  created REAL NOT NULL -- 本记录创建时间，UTC Unix 秒
);
-- calls：一次实际提交请求一行；token 数为供应商返回量，无价格时不把费用视为零。
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  at REAL NOT NULL, -- 实际事件或请求发起时间，UTC Unix 秒
  feature TEXT NOT NULL, -- 模型能力字典：chat、vision、asr、tts
  model TEXT NOT NULL, -- 实际请求的固定模型名称
  endpoint TEXT NOT NULL, -- 实际模型服务地址，不含密钥或请求正文
  success INTEGER, -- 请求结果：NULL 待核对、0 失败、1 成功
  tokens_in INTEGER, -- 供应商返回的输入 token 数，可为空
  tokens_out INTEGER, -- 供应商返回的输出 token 数，可为空
  cached INTEGER, -- 供应商返回的缓存命中 token 数，可为空
  duration REAL, -- 请求实际耗时，秒
  error TEXT NOT NULL DEFAULT '' -- 经过限制的错误类别，不含供应商响应正文
);
CREATE INDEX IF NOT EXISTS call_date ON calls(at,model);
-- state：任务日期、批次、采集位置等可续跑状态，不保存模型密钥。
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY, -- 业务状态唯一键，含任务类型或执行身份
  value TEXT NOT NULL -- 状态 JSON；记录进度、批次和执行结果
);
-- reminders：私聊本人或管理员群提醒；recurrence为日历周期配置JSON。
CREATE TABLE IF NOT EXISTS reminders (
  id TEXT PRIMARY KEY, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  request TEXT NOT NULL UNIQUE, -- 创建提醒的原请求唯一身份，重复消息不重复建任务
  account TEXT NOT NULL, -- 机器人账号的真实稳定微信 ID
  room TEXT NOT NULL, -- 原会话稳定 ID，作为记忆与操作权限边界
  actor TEXT NOT NULL, -- 真实发送者或发起者稳定微信 ID
  content TEXT NOT NULL, -- 已冻结的提醒、分享或发送正文
  due REAL NOT NULL, -- 下次提醒应发送时间，UTC Unix 秒
  recurrence TEXT NOT NULL, -- 周期 JSON：once、interval、daily、weekly，包含时区
  status TEXT NOT NULL, -- 业务进度字典；由代码校验，不使用 CHECK 约束
  revision INTEGER NOT NULL DEFAULT 1, -- 提醒配置修订序号，编辑时递增，阻止旧计划继续发送
  created REAL NOT NULL, -- 本记录创建时间，UTC Unix 秒
  updated REAL NOT NULL -- 最近进度更新时间，UTC Unix 秒
);
CREATE INDEX IF NOT EXISTS reminder_due ON reminders(status,due);
-- deliveries：发送意图先落库，同一key不重新发起；uncertain须核对而非盲重发。
CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  account TEXT NOT NULL, -- 机器人账号的真实稳定微信 ID
  room TEXT NOT NULL, -- 原会话稳定 ID，作为记忆与操作权限边界
  content TEXT NOT NULL, -- 已冻结的提醒、分享或发送正文
  state TEXT NOT NULL, -- 发送或分享状态字典：pending、confirmed、failed、uncertain
  remote TEXT, -- 组件回执 JSON，可为空；用于核对相同发送身份
  created REAL NOT NULL, -- 本记录创建时间，UTC Unix 秒
  updated REAL NOT NULL -- 最近进度更新时间，UTC Unix 秒
);
-- persona_versions：基础/演化/教导的历史，source保存变更证据而非隐私正文。
CREATE TABLE IF NOT EXISTS persona_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  kind TEXT NOT NULL, -- 业务种类字典：消息媒体类型、记忆知识类型或人格变更类型
  body TEXT NOT NULL, -- 消息原文、文字理解结果或记忆正文
  source TEXT NOT NULL, -- 原消息身份 SHA256 摘要，包含账号、会话和组件消息编号
  created REAL NOT NULL -- 本记录创建时间，UTC Unix 秒
);
-- shares：明确请求的单次分享；id对应原请求，receiver限定接收会话。
CREATE TABLE IF NOT EXISTS shares (
  id TEXT PRIMARY KEY, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  account TEXT NOT NULL, -- 机器人账号的真实稳定微信 ID
  owner TEXT NOT NULL, -- 授权本次分享的原说话人稳定微信 ID
  room TEXT NOT NULL, -- 原会话稳定 ID，作为记忆与操作权限边界
  receiver TEXT NOT NULL, -- 本次明确指定的接收会话稳定 ID
  origin TEXT NOT NULL, -- 来源事件编号 JSON 数组，用于追溯、隔离与删除同步
  content TEXT NOT NULL, -- 已冻结的提醒、分享或发送正文
  state TEXT NOT NULL, -- 发送或分享状态字典：pending、confirmed、failed、uncertain
  created REAL NOT NULL -- 本记录创建时间，UTC Unix 秒
);
-- corrections：修正当前原文时保留旧证据，删除时一并清除。
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  event_id INTEGER NOT NULL, -- 对应 events.id；一条原消息只能领取一次对话任务
  body TEXT NOT NULL, -- 消息原文、文字理解结果或记忆正文
  created REAL NOT NULL -- 本记录创建时间，UTC Unix 秒
);
-- sessions：控制台令牌仅保存摘要，csrf与到期时间限定会话。
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, -- 控制台会话令牌 SHA256 摘要，不保存原令牌
  csrf TEXT NOT NULL, -- 该登录会话的随机 CSRF 校验值
  expires REAL NOT NULL -- 登录会话失效时间，UTC Unix 秒
);
CREATE TABLE IF NOT EXISTS login_failures (
  peer TEXT NOT NULL, -- 登录请求来源 IP，仅用于失败限流
  at REAL NOT NULL -- 实际事件或请求发起时间，UTC Unix 秒
);
-- turns：一次合并对话的持久状态；origin为原消息ID数组，response为待发送结果。
CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY, -- 本表记录唯一编号；消息和人格版本为自增编号，其余由业务生成
  account TEXT NOT NULL, -- 机器人账号的真实稳定微信 ID
  room TEXT NOT NULL, -- 原会话稳定 ID，作为记忆与操作权限边界
  origin TEXT NOT NULL, -- 来源事件编号 JSON 数组，用于追溯、隔离与删除同步
  status TEXT NOT NULL, -- 业务进度字典；由代码校验，不使用 CHECK 约束
  response TEXT, -- 已生成且冻结的最终回复 JSON，可为空
  created REAL NOT NULL, -- 本记录创建时间，UTC Unix 秒
  updated REAL NOT NULL -- 最近进度更新时间，UTC Unix 秒
);
-- handled：原消息只能归属于一个对话任务，重启不能重新合并并重复回复。
CREATE TABLE IF NOT EXISTS handled (
  event_id INTEGER PRIMARY KEY, -- 对应 events.id；一条原消息只能领取一次对话任务
  turn_id TEXT NOT NULL -- 关联 turns.id；重启后沿用相同对话任务
);
