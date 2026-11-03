# Plane ↔ Jira: пакет передачи в разработку

Дата: 2026-11-03. Статус: implementation handoff draft.
Основа: [функциональная спецификация](plane-jira-integration-spec.md).
Приложение не изменено, внешние записи не выполнялись.
Раздел 13 уточняет обязательные границы полномочий, UI и критерии приёмки
по результатам независимого чтения; применяется совместно с разделами 2–7.

## 1. Цель и принятые требования

Пользователь подключает личный Jira-аккаунт в Plane, связывает задачи или
создаёт английские Jira-тикеты, настраивает передачу статусов и получает
корректные ветки/PR по связанному Jira-ключу.

Новое требование пользователя: интеграция действует от имени пользователя.
Поэтому общий технический аккаунт из раннего предложения исключён.
Настройки проекта общие, авторизация персональная. Автоматизация не получает
права одного пользователя только потому, что другой изменил локальный тикет.

Документ задаёт реализационные решения по умолчанию. OAuth capability,
схема Jira tenant и фактический development executor должны быть подтверждены
в DEV-01; это технические release gates, не повод оставлять архитектуру неопределённой.
Перед реализацией владелец продукта подтверждает спорные defaults раздела 12.

## 2. Авторизация и исполнение от имени пользователя

### 2.1. Выбранная модель

Основной кандидат для Jira Cloud — персональный OAuth authorization-code grant
с серверным хранением токенов. Применимость, scopes, offline refresh, redirect URI,
регистрация приложения и tenant approval проверяются по официальным документам
и тестовому tenant до реализации transport. Их поддержку не считать уже проверенной.
Не подменять OAuth API token без отдельного решения.

Личный token fallback, если понадобится, разрабатывается отдельным профилем:
ввод только собственного токена, server-side identity verification, secret storage,
те же consent/expiry/access boundaries. В v1 по умолчанию выключен.
Секреты не запрашивать в чат и не использовать credential подключённого чат-коннектора.

Local OAuth callback — отдельный preflight: точный origin/port/path, browser session,
разрешённый redirect и CSRF state. Не делать доступный всему интернету tunnel автоматически.
Если провайдер не допускает выбранный local callback, provisioning блокируется
с конкретным решением владельца окружения; запрещён незаметный обход.

| Событие                                                      | Кто исполняет                                               | Когда блокировать                                            |
| ------------------------------------------------------------ | ----------------------------------------------------------- | ------------------------------------------------------------ |
| Link/search/schema preview                                   | Текущий Plane user со своим Jira grant                      | Нет grant или доступа к объекту                              |
| Confirm Create in Jira                                       | Тот же пользователь, который подтвердил snapshot            | Чужой draft approval, changed payload или revoked grant      |
| Ручной Plane → Jira transition                               | Инициатор локального изменения, его личный grant            | Нет разрешения или grant; локальное изменение сохранить      |
| Входящий polling Jira → Plane                                | Явный automation owner связи                                | Нет consent, membership или возможности изменять Plane issue |
| Исходящее событие локального бота/chat workflow              | Automation owner только для разрешённых типов system events | Нет отдельного consent; LLM Complete не закрывает Jira       |
| Изменение локального статуса другим человеком без Jira grant | Не использовать grant automation owner                      | `actor_not_connected`, явная ручная reconciliation           |
| Resolve conflict                                             | Текущий пользователь с собственными правами обеих сторон    | Изменилась revision после preview                            |
| Ветка/PR                                                     | Пользователь выбранного development adapter                 | Jira grant не даёт GitHub/git полномочий                     |

Automation consent действует только на выбранный binding, направления и
типы событий; содержит owner, version, granted_at, revoked_at, scope hash.
Owner выбирает checkbox «Разрешить фоновую синхронизацию этой задачи от моего имени»
с точным описанием разрешённых действий. По умолчанию выключен.
Project admin может приостановить автоматизацию, но не назначить чужой consent.

Перед каждым dispatch проверять: grant version, owner active membership,
issue permissions, policy version, binding generation, consent version и
provider permission. Подтверждение не даёт бессрочного права на иной payload.
Permission errors никогда не приводят к fallback на другой аккаунт.

### 2.2. Lifecycle

1. Connect инициируется authenticated POST; одноразовый OAuth state связан
   с пользователем/workspace/session, имеет TTL и фиксированный callback.
2. Callback обменивает code серверно; проверяет state, ожидаемую session,
   identity и доступные sites. Replay, mismatch и open redirect отклоняются.
3. Пользователь подтверждает Jira account/site. Сохраняются stable account ID,
   encrypted tokens, expiry/scopes и grant version, а не пароль.
4. Refresh сериализован по grant. Новый rotating credential сохраняется атомарно.
   Потеря ответа refresh не вызывает бесконтрольный параллельный повтор:
   reconciliation по поддерживаемому контракту или reconnect required.
5. Reconnect того же account обновляет grant version. Иной account — явная
   account replacement, consent и pending writes аннулируются.
6. Disconnect останавливает локальное использование немедленно, инвалидирует
   pending jobs и consent; remote revoke выполняется только если поддержан.
   UI различает local disconnected и provider revocation unconfirmed.
7. Удаление пользователя/workspace membership прекращает его автоматизацию.
   Новый owner должен отдельно подключиться и подтвердить consent.
8. Browser logout не равен disconnect: UI явно сообщает, что ранее разрешённая
   фоновая синхронизация продолжается; предоставляет действие Pause all mine.

### 2.3. Видимость и утечки

Jira-текст, title, comment, поля и ссылки, прочитанные через grant A, не выдаются
пользователю B только по праву читать Plane. В v1 показ fetched Jira metadata
требует личной успешной access check B; при отсутствии её карточка сообщает
«Jira подключена; подключите аккаунт для просмотра» без закрытых подробностей.
Кэш access decisions привязан к grant/version, короткоживущий; отзыв доступа
не обеспечивает мгновенного удаления уже увиденного, но новые ответы блокируются.

Shared Plane state, изменённый из Jira, по определению раскрывает часть workflow
участникам Plane. Поэтому inbound consent включает явное предупреждение об этом.
Restricted Jira issues с несовместимой аудиторией не подключаются к inbound sync
в v1: требуется подтверждённое разрешение публикации статуса в Plane.
Описания Jira автоматически в shared Plane не импортируются.
Уникальность чужого binding сообщается без раскрытия названия недоступного issue.
Detailed audit и operation payload доступны инициатору/разрешённому reviewer
после access check; администратору — sanitized health, не содержимое чужих тикетов.

## 3. Интерфейс: состав экранов и поведение

Это контракт интерфейса, не готовый макет и не реализация. Использовать существующие
settings, Propel primitives, Plane blocks, MobX shared-state и RU/EN localization.
Desktop: форма с ограниченной шириной, tabs сверху; узкий экран: одна колонка.
Никаких скрытых действий по сохранению и автозапуска интеграции при открытии экрана.

| Экран                           | Содержимое сверху вниз                                                         | Основное действие                                 |
| ------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------- |
| Profile → Integrations → Jira   | Account/site; health; scopes; last checked; мои automation bindings            | Connect / Reconnect / Disconnect / Pause all mine |
| Workspace → Integrations → Jira | Allowed site; app provisioning readiness; health counters; policy restrictions | Enable site / Pause workspace                     |
| Project → Settings → Jira       | Tabs: General, Statuses, Ticket template, Development, Diagnostics             | Save draft → Validate → Enable                    |
| Issue details → Jira card       | Key и status с observed_at; mode/owner; operation state; actions               | Link existing / Create Jira / Configure sync      |
| Issue → Development             | Repo, branch, PR title/state/head; workflow readiness                          | Prepare branch / Prepare PR / Attach existing     |
| Integration operations          | Filters: mine/project/status; paginated operations; actionable reason          | Inspect / Reconcile / Safe retry                  |

### 3.1. Project setup wizard

1. **Account & site:** показать «Вы подключены как …»; Connect если grant отсутствует.
   Общая policy не содержит токена и не назначает credentials другим участникам.
2. **Destination:** Jira project обязателен, board опционален для навигации и
   проверки видимости, issue type default. Search/pagination; нет свободного ввода ID.
3. **Statuses:** mode default off; rows source → target для каждого issue type.
   Показывать enabled/invalid/unmapped; отдельные close/reopen checkboxes.
4. **Template:** Jackrabbit English-only version, required fields defaults,
   rendered sample из явно выбранной разрешённой задачи; ничего не отправлять.
5. **Development:** repo profile, branch source, PR target, naming preview,
   executor availability. Jira настройка доступна и без GitHub adapter.
6. **Review:** diff старой/новой policy, затронутые bindings, warnings,
   capability check timestamp; Save draft отдельно от Enable.

Project default определяет поведение новых bindings; существующие не получают
новые write permissions автоматически. Изменение effective policy требует
preview затронутых связей, новой версии и повторного consent при расширении scope.
Невалидный mapping блокирует enable соответствующего направления, но не обычный link.

### 3.2. Status editor

Таблица: Plane state, Jira target, direction, terminal meaning, validation.
Dropdown содержит только реальные IDs с labels. Имена и порядок колонок
не являются matching keys. LLM Work и In Progress могут иметь общий Jira target,
но reverse target выбирается ровно один. LLM Complete недоступен как inbound target.

Нижний блок: Close enabled, Reopen enabled, Manual hold protected (не отключается),
Conflict handling = Ask user (не last-write-wins), Preview on example issue.
Preview показывает actual available transitions для примера, не обещает,
что они доступны для всех задач и всех пользователей.

### 3.3. Link/create dialogs

Link: URL/key → найденный разрешённый issue → preview → Link.
Off default; status sync отдельное действие после успешной привязки.
Если key ambiguous по site — выбор site, не угадывание.

Create: слева редактируемые English Summary/Description, справа destination,
type и динамические required fields. На узком экране блоки вертикальны.
Footer показывает «Будет создано от имени {account} в {project}».
Кнопки Generate draft, Validate, Create in Jira. Последняя доступна только
для валидированного текущего snapshot и явно подтверждённого пользователя.
Подтверждение создаёт operation; закрытие dialog не отменяет отправленную операцию.
После timeout показать receipt/unknown и Reconcile, не новую кнопку Create.
Required field зависимости пересчитываются при смене project/type/parent.
Поле reporter не заполняется произвольным аккаунтом, assignee — только явный выбор.

### 3.4. Issue card, конфликт и диагностика

Badge состояния: Not linked / Linked, sync off / Synced / Pending /
Needs connection / Conflict / Unavailable / Outcome unknown / Paused / Stale.
Цвет сопровождается текстом и иконкой. Tooltip поясняет observed_at и executor.
Local state и remote state отображаются отдельно.

Conflict dialog: baseline, current Plane, current Jira, actor/time если доступны,
mapping и reason. Кнопки Keep separate, Apply Plane to Jira, Apply Jira to Plane.
Для каждой — fresh preview, permission check и confirm; при новой revision → 409,
диалог обновляется и требует повторного решения. Никакой кнопки Force без проверок.

Diagnostics показывает last successful observation, queue delay, policy/grant
versions без секретов, blocked reason, next retry, correlation ID. Текст ошибки
объясняет действие пользователя: reconnect, fix mapping, request access, reconcile.
Missing actor grant не отображать как общую недоступность Jira.

## 4. Настраиваемые параметры

Значения ниже — проектные defaults и ограничения продукта, не лимиты Jira.
Client/server limits провайдера имеют приоритет и уточняются в DEV-01.

| Параметр                        | Уровень / кто меняет         | Default / validation                                                                         |
| ------------------------------- | ---------------------------- | -------------------------------------------------------------------------------------------- |
| Jira site                       | Workspace admin              | Только verified allowlisted Cloud site                                                       |
| OAuth client ID/secret/callback | Operator deployment          | Secret server-side; callback exact match                                                     |
| Personal account                | Пользователь                 | Stable account ID; нельзя выбрать чужой grant                                                |
| Connection scope                | Workspace + user + site      | Не использовать grant за пределами scope                                                     |
| Project / board                 | Project admin                | Project required; board optional, compatibility warning                                      |
| Default issue type              | Project admin                | Реальный доступный type; без hardcoded ID                                                    |
| Field defaults                  | Project admin                | Typed values по schema; no arbitrary script                                                  |
| Template                        | Project admin                | Versioned Jackrabbit English-only                                                            |
| Description size                | Server policy                | 30,000 characters максимум после conversion как project limit                                |
| Sync default                    | Project admin                | off                                                                                          |
| Binding direction               | Member с issue edit          | inherit/off/one-way/bidirectional в разрешённых policy пределах                              |
| Automation owner                | Сам согласившийся member     | unset; admin не даёт consent за другого                                                      |
| System event export             | Consent owner                | off; explicit allowlist типов событий                                                        |
| Auto-close / reopen             | Policy + binding consent     | off/off; Done mapping включается отдельно                                                    |
| Conflict policy                 | Фиксировано v1               | Manual resolution                                                                            |
| Poll interval                   | Operator                     | 60 s; range 30–3600; jitter ±20%                                                             |
| Metadata cache TTL              | Operator                     | 900 s; invalidate on schema failure                                                          |
| Access-check cache TTL          | Operator                     | 60 s максимум для отображения; fresh authorization на write                                  |
| Stale indicator                 | Operator                     | После 3 poll intervals без успеха                                                            |
| Worker concurrency              | Operator                     | 2 на site, 1 mutation на binding                                                             |
| GET retry                       | Operator                     | До 5 попыток; backoff 2/4/8/16/32 s + jitter, Retry-After приоритетен                        |
| POST unknown                    | Фиксировано v1               | No blind retry; reconcile                                                                    |
| Reconcile unknown               | Operator                     | До 3 read-only проходов за 10 min, затем manual review; POST не разблокируется автоматически |
| Local request timeout           | Operator                     | connect 5 s/read 30 s; не доказывает remote failure                                          |
| Draft approval TTL              | Operator                     | 30 min плюс инвалидирование по hash/version                                                  |
| OAuth state TTL                 | Operator                     | 10 min, одноразовый                                                                          |
| Repo / branch source / PR base  | Project admin                | Jackrabbit profile: master / staging; verify exact repo                                      |
| Branch / PR template            | Project admin                | `{jira_key}-{slug}` / `{jira_key}: {summary}`                                                |
| Executor                        | Operator + project allowlist | Existing Jackrabbit adapter; not configured блокирует execute                                |
| Translation provider            | Operator + user data consent | Disabled до выбора; ручной английский draft доступен                                         |
| Audit/payload retention         | Operator                     | 180/30 days; отдельно согласовать требования организации                                     |

Нет end-user настроек для отключения TLS, SSRF checks, permission checks,
secret redaction, finalizer protection или unknown-create safeguard.
Эффективная конфигурация видна вместе с источником значения и version.

## 5. Backend контракт и технические изменения

К исходной модели добавить `JiraUserGrant`, `JiraAutomationConsent`,
`OAuthAttempt` (одноразовый state digest, TTL), operation actor/grant/consent
versions и per-principal observation/access cache. Site identity отделить от grant:
один Jira binding не дублируется при подключении второго пользователя.

`IntegrationOperation` хранит initiating_user, executing_user, Jira account ID,
grant reference/version, consent version (если требуется), origin и payload hash.
Не хранить token в job аргументах. Выбор executor — серверный resolver по таблице
раздела 2, а не пользовательский arbitrary user_id.

Дополнительные API, все под существующим auth/CSRF контрактом:

| Route intent                         | Input                                    | Result                                                  |
| ------------------------------------ | ---------------------------------------- | ------------------------------------------------------- |
| Personal authorize                   | workspace/site, fixed return location ID | authorization URL + expiring attempt                    |
| OAuth callback                       | code/state                               | Verified identity/site selection; no tokens to frontend |
| Personal disconnect                  | grant ID/version, confirm                | Local disabled + affected bindings count                |
| Binding consent preview/grant/revoke | binding generation, scope/version        | Exact allowed actions + receipt                         |
| Policy validate/enable               | expected version, draft policy           | Field-level errors или new policy version               |
| Draft validate/approve/submit        | draft hash/revision, idempotency key     | 202 operation; wrong actor/hash → 409/403               |
| Operation reconcile                  | operation ID/version                     | Fresh read-only result; no new create                   |
| Ownership transfer accept            | binding version, own grant, consent      | Old jobs invalidated; fresh baseline                    |

Operation lifecycle: prepared → queued → dispatching → verifying → succeeded.
Дополнительные terminal/paused outcomes: cancelled_before_dispatch, blocked_auth,
blocked_policy, conflict, outcome_unknown, failed_validation, failed_definitive.
Только ошибки, достоверно предшествующие внешней записи, допускают новый dispatch.
Retry transport и новый business intent — разные понятия и разные audit actions.

Не привязывать receipt retention к возможности повторного create: dedup tombstones
и external identity живут дольше payloads. Повтор старого idempotency key после
очистки payload не должен снова создавать удалённый тикет.

### Области кодовой базы

| Область                                                     | Работа                                                                  |
| ----------------------------------------------------------- | ----------------------------------------------------------------------- |
| `apps/api/plane/db/models/integration/`                     | Site, personal grant, consent, bindings, operations, constraints        |
| `apps/api/plane/db/migrations/`                             | Additive schema migrations, без включения sync существующим проектам    |
| `apps/api/plane/app/views/` и `serializers/`, `urls/`       | Auth, policy, link/draft/operation endpoints                            |
| Новый domain/service package в `apps/api/plane/`            | Pure planner, auth resolver, provider adapter, reconciliation           |
| `apps/api/plane/bgtasks/`                                   | Outbox dispatch, polling, refresh coordination                          |
| `apps/api/plane/db/models/issue.py` и текущие state writers | Inventory, transactional events, соблюдение completion/manual hold      |
| `packages/types/`                                           | DTO/enums/reason codes; tokens никогда не включаются                    |
| `packages/shared-state/`                                    | Per-user/per-project stores; clear user caches on logout/account switch |
| `apps/web/services/integrations/`                           | Новый Jira sync client отдельно от JiraImporterService                  |
| `apps/web/components/settings/profile/`                     | Личные Integrations, connect lifecycle                                  |
| Workspace integrations route и Project settings             | Site/policy wizard, editor, diagnostics                                 |
| Issue details components                                    | Jira/development cards, link/create/conflict dialogs                    |
| `packages/i18n/`                                            | RU/EN labels, объяснимые error reasons                                  |

Точные новые filenames выбираются при реализации по conventions.
Не считать существующие importer endpoints или GitHub issue-sync модели готовым
решением для OAuth и pull requests.

## 6. Backlog для разработки

ID ниже локальные, не созданные Jira tickets. Owners — роли, не назначенные люди.

| ID     | Пакет / результат                                                                     | Зависимости      | Owner                        | Gate                                                          |
| ------ | ------------------------------------------------------------------------------------- | ---------------- | ---------------------------- | ------------------------------------------------------------- |
| DEV-01 | Capability manifest: OAuth/local callback/scopes, Jira schema, sandbox, repo executor | —                | Backend + operator           | Реальные read-only evidence; явные unsupported cases          |
| DEV-02 | Migration/models/constraints, operations/outbox, feature flags                        | DEV-01 contracts | Backend                      | DB concurrency + migration tests                              |
| DEV-03 | Personal OAuth lifecycle, refresh/revoke, actor resolver                              | DEV-02           | Backend                      | Auth/tenant/rotation/replay tests                             |
| DEV-04 | Personal integration screen + workspace site health                                   | DEV-03 DTO       | Frontend                     | Connect/reconnect/disconnect и no-secret snapshots            |
| DEV-05 | Metadata/schema adapter + project setup/status editor                                 | DEV-03           | Backend + frontend           | Typed dynamic fields, version conflicts, capabilities by user |
| DEV-06 | Link/unlink, cards, access-aware reads                                                | DEV-05           | Backend + frontend           | Duplicate binding, forbidden metadata, no remote write        |
| DEV-07 | English draft/validation/approval/create/readback/recovery                            | DEV-06           | Backend + frontend           | Unknown create и double-click; one remote issue               |
| DEV-08 | Shared state event path, pure planner, outbox executor                                | DEV-02, DEV-05   | Backend                      | Все state writers, no loops, no finalizer regression          |
| DEV-09 | Consent UI, one-way sync, polling, pause/reconnect                                    | DEV-06, DEV-08   | Backend + frontend           | Actor boundaries, offline/restart, readback                   |
| DEV-10 | Bidirectional conflict/close/reopen, manual holds                                     | DEV-09           | Backend + frontend           | Race matrix, canonical reverse mappings                       |
| DEV-11 | Naming/DevelopmentPlan, existing executor adapter, PR card                            | DEV-06, DEV-01   | Developer tooling + frontend | Реальное approved branch/PR creation и receipt                |
| DEV-12 | Diagnostics/runbook/security/performance/end-to-end rollout                           | DEV-04–11        | QA + backend + operator      | Матрица требований закрыта доказательствами                   |

Разумные PR-пакеты: foundations; auth; settings/link; draft/create;
state-engine; consent/one-way; conflicts; development adapter; rollout.
Каждый PR отдельно отключаем feature flag. Внешние записи не включаются
до actor/consent/security gates. Frontend и pure planner можно вести независимо;
одновременно менять общий state writer несколькими исполнителями нельзя.

## 7. Проверка корректности и дополнительные критерии приёмки

AC01–AC20 основной спецификации сохраняются. Ниже дополнительные требования
личной авторизации и UI. Считать тест выполненным только при наличии receipt.

| ID   | Given / When                                       | Then / доказательство                                                                    |
| ---- | -------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| AC21 | A подключён; B меняет Plane state без своего grant | Jira не меняется, actor_not_connected; токен A не использован                            |
| AC22 | Admin пытается выполнить действие с grant B        | 403; никаких внешних вызовов                                                             |
| AC23 | Пользователь подтвердил create                     | Provider actor соответствует его account ID; UI и audit совпадают                        |
| AC24 | Consent owner удалён из проекта                    | Pending/background writes blocked; local issue остаётся доступным остальным              |
| AC25 | OAuth state replay/mismatch/expired                | Callback отклонён, grant не создан                                                       |
| AC26 | Два workers одновременно refresh                   | Один refresh exchange; атомарное сохранение или reconnect, без потери токена гонкой      |
| AC27 | Reconnect другим Jira account                      | Явное подтверждение; старые consent/operations не продолжаются                           |
| AC28 | B не имеет доступа к Jira issue A                  | Нет title/description/key leakage в card/search/audit/errors/cache                       |
| AC29 | Policy расширяет close/system-event scope          | Старое согласие не покрывает расширение, affected bindings paused                        |
| AC30 | Logout при действующем consent                     | Фоновая работа только согласно заранее видимому consent; Pause all mine её останавливает |
| AC31 | Project mapping сохранён без Enable                | Никаких внешних status writes                                                            |
| AC32 | User меняет project/type после draft validation    | Поля пересчитаны, approval сброшен, stale submit отклонён                                |
| AC33 | Unlink и новый link во время queued operation      | Старый generation не может изменить новый Jira issue                                     |
| AC34 | Execute PR без adapter/прав/required gates         | Невозможно; Prepare plan не выдаётся за созданный PR                                     |
| AC35 | Полный frontend workflow                           | Keyboard navigation, field errors, pending state, RU/EN, narrow layout проверены         |
| AC36 | Worker crash после remote success до local receipt | Readback/reconciliation восстанавливает одну операцию без дубля                          |
| AC37 | Истёк payload retention, повторён старый key       | Dedup сохраняется; второй Jira issue не создаётся                                        |
| AC38 | Workspace/site pause при in-flight POST            | UI предупреждает; запрет новых dispatch, фактический результат прочитан                  |

### Тестовые уровни

1. Unit: state planner, permissions resolver, policy scope hashes, naming,
   ADF/template validation, redaction, typed field mapping.
2. Transaction tests: race link/create, outbox atomicity, grant refresh lock,
   lock ordering с existing chat workflow, consent revocation during dispatch.
3. Provider contract: recorded sanitized fixtures и fake transport; 400/401/403/
   404/409/429/5xx, pagination, schema change, invalid transition, lost POST response.
4. UI integration: fake backend для всех card/dialog states; access isolation,
   two tabs optimistic version conflict, reconnect и stale metadata.
5. Existing regression: chat/finalizer, imports, state/completed_at/activity,
   board realtime updates, unrelated integrations.
6. Sandbox E2E: личные тестовые A/B, разные Jira permissions, реальные
   create/link/status/PR readback. Только после разрешения на конкретные записи.

### Предлагаемые performance targets для измерения

На зафиксированном стенде: 1,000 active bindings, 10 grants, 2 sites;
mock provider с заданной latency и отдельный малый live canary.
Local async request p95 <500 ms без provider wait; нет N+1 credentials/metadata;
очередь после 5 минут offline догоняется без превышения limiter.
Для 10 canary bindings при healthy provider обновление карточки ≤2 poll intervals
в 95% наблюдений. Эти цели измерить, не объявлять подтверждёнными заранее.
HTTP rate budget определяется реальным provider contract, не числом workers.

### Run matrix и доказательства

QA ведёт таблицу: requirement ID → automated test ID → run/commit SHA →
result passed/failed/skipped → receipt/screenshot → known limitation.
Точные команды брать из AGENTS и RUNNING_TESTS, framework UI проверить на DEV-01.
Минимум: backend targeted и full relevant suite, pnpm lint/types/format,
frontend tests, verified screenshots фактических экранов и sandbox readbacks.
Mock screen или документация не заменяют screenshot работающего приложения.

## 8. Полный приёмочный маршрут

1. Admin разрешает site; A и B подключают свои аккаунты; каждый видит свой account.
2. A настраивает project policy как draft; validate; сохраняет без sync.
3. A связывает существующий Jira issue; ни один status не меняется.
4. B открывает card без права Jira: metadata скрыта. С разрешённым grant видна.
5. A создаёт English draft; обязательное поле пропущено → Create disabled.
6. A заполняет поле, подтверждает payload; double-submit даёт один receipt/issue.
7. A разрешает one-way consent; ручной state change применён с actor A и readback.
8. B меняет state без grant: локально сохранено, Jira не изменена чужим аккаунтом.
9. Simultaneous change обеих сторон → conflict; resolution после fresh preview.
10. LLM Complete не закрывает Jira. Manual hold не перезаписывается.
11. Разрешённый Done закрывает Jira только валидным transition; reopening off.
12. Disconnect A останавливает pending sync; reconnect не воспроизводит старые writes вслепую.
13. Branch/PR plan использует реальный key; approved executor создаёт объекты,
    readback подтверждает repo/base/head/title. Merge не выполняется.
14. Pause/unlink сохраняют Jira; все действия и причины доступны в permitted audit.

Сбой любого security/unknown-create/finalizer шага блокирует выпуск write feature.
Sandbox cleanup согласовать отдельно; тесты не удаляют объекты production.

## 9. Rollout, обслуживание и откат

Feature flags независимы: personal_auth, link, create, outbound_sync,
inbound_sync, bidirectional_sync, development_execute. Default false.
Этапы: internal read-only → один личный sandbox grant → link/create canary →
one-way consent → bidirectional → development adapter.

Не мигрировать старые shared credentials в personal grants автоматически.
Существующие importer integrations не менять. Для старых bindings, если появятся,
actor/consent migration оставляет sync off до личного reconnect.

Kill switch останавливает новый dispatch; in-flight операции сверяются.
Rollback релиза оставляет durable receipts и ciphertext/ключ совместимыми;
revoke/disconnect сохраняют минимальный audit по retention policy.
Alert на unknown create, auth failure burst, queue age и stale bindings.
Ошибки токенов одного пользователя не отключают grants остальных.

## 10. Definition of Ready / Definition of Done

Ready для foundational разработки: этот пакет, API DTO/state contracts,
scope и AC приняты; unresolved provider capabilities отмечены явными spikes.
Ready для OAuth/provider implementation: DEV-01 manifest с доказательствами,
выбранный разрешённый callback и scopes, sandbox/test identities доступны.
Ready для Git execute: существующий Jackrabbit entry point и gates проверены.

Done: реализация, migrations, permission matrix, все обязательные AC,
regression checks, реальные UI screenshots, sandbox receipts, runbook,
feature flags и rollback проверены. Нет unresolved Critical/High findings.
Skipped live E2E означает «готово локально, live verification pending»,
а не полностью завершённую интеграцию.

## 11. Передача исполнителю

Реализовывать только после отдельной команды на код. Передать оба документа,
начать с DEV-01 и согласования DTO, затем DEV-02/03. Не создавать Jira issues,
PR, grants или OAuth приложения только на основании этого handoff.
Не считать файлы спецификации разрешением на использование credentials.

Каждый пакет сдаётся с изменёнными файлами, точными командами проверок,
результатами/пропусками, screenshots/readbacks по необходимости, рисками
и следующим gate. Commit identity только пользователя; агентского credit нет.

## 12. Что остаётся подтвердить

| Решение                             | Предлагаемый вариант                                               | Когда требуется   |
| ----------------------------------- | ------------------------------------------------------------------ | ----------------- |
| Реальный site/project/board и ключ  | Доступный пользователю Jira Cloud; реальный FP/FS key              | DEV-01            |
| OAuth registration и local callback | Personal OAuth, без общего service account                         | До DEV-03         |
| Кто может запускать фоновые события | Self-consent owner; чужие human edits не исполняются его grant     | До DEV-09         |
| Начальный sync/closure              | off; затем Plane → Jira, Done по отдельному consent                | До включения sync |
| Executor branch/PR                  | Existing Jackrabbit adapter; GitHub identity пользователя отдельно | До DEV-11         |
| English-only или bilingual extras   | English-only Description; Russian Developer Details не обязательно | До DEV-07         |

Спецификация пригодна для декомпозиции и реализации после проверки Ready.
Она не утверждает, что live provider contract уже проверен или что найден
готовый исполнитель PR. Эти неизвестные локализованы в отдельных gates.

## 13. Уточнения после проверки спецификации

Это исправления проектного контракта, не результаты тестирования приложения.
Закрывают замечания F1–F5; новые критерии входят в обязательную матрицу приёмки.

### 13.1. Кто изменил состояние, кто дал полномочия и кто исполнил

| Поле аудита             | Значение                                                                               | Запрещённая подмена                                            |
| ----------------------- | -------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `jira_actor`            | Подтверждённый автор удалённого изменения, если доступен; иначе unknown                | Не угадывать по assignee, credential owner или времени polling |
| `initiating_plane_user` | Человек, непосредственно инициировавший локальное действие; null у фонового наблюдения | Не ставить owner как инициатора удалённого изменения           |
| `credential_owner`      | Владелец личного grant, использованного для запроса                                    | Не приравнивать к автору наблюдаемого Jira-события             |
| `executing_actor`       | Локальный integration worker с operation ID и origin                                   | Не изображать worker человеком в activity feed                 |
| `authority_owner`       | Человек, чьи актуальные права и consent разрешили фоновое действие                     | Не брать права remote Jira actor как права Plane               |
| `source_event`          | ID/fingerprint и тип наблюдения, без выдуманного автора                                | Poll snapshot не выдаётся за точное событие changelog          |

Для прямого create initiating_plane_user = credential_owner; executing_actor
остаётся worker. Jira readback проверяет identity доступного фактического автора,
а если API не предоставляет её — receipt явно указывает предел проверки.
Для polling initiating_plane_user = null, authority_owner = consent owner;
jira_actor может быть другим человеком, bot, non-member Plane или unknown.

Входящий sync переносит актуальное состояние Jira, а не выполняет инструкции
её автора. Поэтому Jira actor не обязан состоять в Plane: локальное разрешение
даёт только consent owner с актуальным правом изменять этот Plane issue,
при действующих inbound scope, mapping, аудитории и protected-state checks.
В consent/UI явно написать: «Синхронизировать разрешённые изменения статуса
в Jira, в том числе выполненные другими пользователями или автоматизацией».
Не пытаться реализовать actor allowlist на основании одного polling snapshot.
Если нужен такой фильтр, это отдельная функция с доказуемым changelog coverage.

Для локальных system events источник должен быть типизированным и доверенным
сервером; нельзя передать `origin=system` с клиентского API и получить grant owner.
Human event без grant остаётся blocked, а не переклассифицируется в bot event.
Activity feed: «Jira integration изменила статус по согласию A; автор Jira B /
неизвестен». Если старый activity schema требует user FK, технический FK owner
не меняет отображаемое авторство: typed origin и полная attribution обязательны.

### 13.2. Какие изменения аннулируют согласие

Вычислять отдельные scope hashes для Jira automation и development execution.
Каноническая сериализация, stable IDs, версия схемы hash; labels и порядок
ключей не меняют смысл. Изменения отслеживать по effective values каждого binding.

| Изменение                                                                    | Обязательное поведение                                                                                                         |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Workspace/site, target Jira project, binding issue identity                  | Остановить старые jobs; новый target — explicit rebind, не тихая retarget policy                                               |
| Issue type / parent routing                                                  | Инвалидировать create draft и pending create; для существующего issue revalidate mapping, не менять тип удалённо автоматически |
| Direction, status mapping, terminal meaning, close/reopen                    | Новый automation scope; отменить неотправленные старые jobs; новое согласие affected owners                                    |
| Разрешённые system events, inbound audience publication                      | Новый automation scope и explicit re-consent                                                                                   |
| Owner/account/grant replacement или потеря прав                              | Pause, fresh authorization, acceptance новым владельцем; старые jobs не передавать                                             |
| Executor/allowlist, repository, branch source, PR base, naming/body template | Инвалидировать development plan/approval; не отключать несвязанную Jira-синхронизацию                                          |
| Description template/required field defaults                                 | Инвалидировать затронутые drafts/approvals; существующее описание не перезаписывать                                            |
| Poll interval, cache TTL, display label                                      | Обновить operational config; не запрашивать consent без изменения полномочий                                                   |
| Disable/revoke/сужение разрешений                                            | Ограничение вступает немедленно; старые разрешения не действуют во время ожидания подтверждения                                |

Даже изменение mapping без добавления направления меняет действие и требует
re-consent. Default policy changes не перепривязывают существующие issues.
При изменении policy version jobs со старой версией до dispatch отменяются
или перепланируются только после проверки неизменности их scope и текущего
состояния; semantic changes никогда не «обновляются» в старом approval.
У affected bindings UI показывает diff и paused_pending_consent.
Не затронутые effective scope bindings сохраняют разрешения.
In-flight mutation не отменяется обещанием UI: outcome сверяется отдельно.

### 13.3. Матрица прав и управление паузой

Роли ниже проверяются в соответствующем workspace/project. Workspace admin
не получает автоматически права project admin или доступ к содержимому задач.
Для всех действий с issue требуется его доступность и соответствующее право;
отсутствие явного allow означает deny.

| Действие                                           | Viewer                                | Member с issue edit                              | Project admin                | Workspace admin                        |
| -------------------------------------------------- | ------------------------------------- | ------------------------------------------------ | ---------------------------- | -------------------------------------- |
| Собственный connect/reconnect/disconnect           | Да, если membership активен           | Да                                               | Да                           | Да                                     |
| Использовать чужой grant или создать чужой consent | Нет                                   | Нет                                              | Нет                          | Нет                                    |
| Allowed site / workspace kill switch               | Нет                                   | Нет                                              | Нет                          | Да                                     |
| Project policy / project pause                     | Нет                                   | Нет                                              | Да                           | Только при наличии project-admin права |
| Link/create/unlink/resolve/development execute     | Нет                                   | Да, со своим grant и отдельными внешними правами | Те же issue/grant требования | Те же issue/grant требования           |
| Дать background consent / принять ownership        | Нет                                   | Только за себя, со своим grant                   | Только за себя               | Только за себя при наличии issue edit  |
| Отозвать свой consent                              | Да, даже после утраты edit            | Да                                               | Да                           | Да                                     |
| Приостановить все свои automations                 | Да                                    | Да                                               | Да                           | Да                                     |
| Возобновить чужой revoked consent                  | Нет                                   | Нет                                              | Нет                          | Нет                                    |
| Просмотр закрытого Jira metadata                   | Только после собственной access check | То же                                            | То же                        | То же                                  |

Project settings → Jira содержит заметный блок «Приостановить интеграцию проекта»,
affected binding count, причину и дату. Pause запрещает dispatch, сохраняя
настройки и receipts. Resume администратора снимает только project pause:
revoked/missing consent, reconnect requirement и conflicts не снимаются.
Workspace pause действует поверх project resume. Confirmation перечисляет
in-flight operations, которые остановить уже нельзя.

### 13.4. Потеря владельца и восстановление

Карточка получает `Automation owner unavailable`, причину без персональных
закрытых данных и кнопку «Принять управление» для eligible member.
Бывший owner показывается только если его identity разрешено видеть в Plane;
иначе «Недоступный участник». Для администратора есть список affected bindings
без закрытого Jira-содержимого и действие pause/request takeover, не assign consent.

Recovery: личный connect нового владельца → проверка доступа к обеим задачам →
preview старой/новой identity и scope → self-consent → fresh baseline →
explicit reconciliation при расхождении → resume.
Только один takeover выигрывает optimistic binding version; второй получает 409.
Старые queued jobs отменяются. Unknown/in-flight операции старого владельца
сначала сверяются read-only; новая запись блокируется до разрешения исхода.
Новый grant не используется для повторения старого create.
Отсутствие преемника оставляет связь paused без удаления Jira.

### 13.5. Setup readiness до кнопки Connect

Backend выдаёт список blockers с reason_code, responsible_role и safe action;
UI показывает их до OAuth redirect, а не только после ошибки.
Состояния можно сочетать: устраняется первый blocking prerequisite,
остальные остаются видимыми. Секреты и внутренняя конфигурация не включаются.

| Состояние                         | Кто исправляет                              | UI / разрешённое действие                                              |
| --------------------------------- | ------------------------------------------- | ---------------------------------------------------------------------- |
| `workspace_site_disabled`         | Workspace admin                             | «Jira не разрешена в workspace»; попросить включение                   |
| `site_not_allowlisted`            | Workspace admin                             | Выбрать разрешённый site или запросить его добавление                  |
| `oauth_app_unconfigured`          | Operator                                    | Инструкция provision app; Connect disabled                             |
| `callback_unavailable`            | Operator                                    | Указать безопасное описание origin/callback mismatch; Connect disabled |
| `tenant_approval_required`        | Jira tenant admin                           | Объяснить необходимость approval; не повторять connect по таймеру      |
| `personal_grant_missing`          | Пользователь                                | Connect разрешён при готовых prerequisites                             |
| `scope_missing` / `grant_expired` | Пользователь / admin при tenant restriction | Reconnect с точным объяснением требуемой capability                    |
| `ready`                           | Пользователь                                | Connect либо Test; project Enable отдельно проверяет policy            |

Readiness не доказывает runtime permission и не заменяет проверки dispatch.
Неизвестную причину provider denial показывать как unclassified error с
correlation ID, а не уверенно как «tenant approval missing».

### 13.6. Дополнительные acceptance gates

| ID   | Сценарий                                                                | Ожидаемое доказательство                                                                                     |
| ---- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| AC39 | Jira state изменён другим member, non-member, bot или неизвестным actor | Перенос только по действующему owner consent; audit различает origin/authority/actor, не выдумывает identity |
| AC40 | Client подставляет system origin / owner уже потерял права              | Нет внешней записи и нет обхода через bot classification                                                     |
| AC41 | Поочерёдно меняется каждый authority-bearing параметр §13.2             | Old jobs остановлены, hash/version обновлены, affected consent/approval требуется заново                     |
| AC42 | Меняется только operational parameter или чужой unaffected scope        | Разрешения несвязанных bindings сохранены; никаких новых writes из backlog                                   |
| AC43 | Каждая forbidden клетка role matrix вызывается напрямую через API       | 403/безопасный not-found; внешних запросов записи нет, UI не служит единственной защитой                     |
| AC44 | Project pause → workspace pause → project resume                        | Workspace restriction сохраняется; чужой revoked consent не восстанавливается                                |
| AC45 | Owner удалён; два участника одновременно принимают ownership            | Один winner, один 409; старые jobs отменены, fresh consent/baseline, без переноса unknown writes             |
| AC46 | Каждый setup blocker §13.5 и неизвестная provider error                 | Правильный responsible role/action, no-secret response, Connect/Enable корректно блокируются                 |

DEV-03 покрывает AC39–40 и grant semantics; DEV-05/08/09 — AC41–44;
DEV-09 — AC45; DEV-01/04 — AC46. Входящий и system-event sync нельзя
включать до AC39–40; developer execute — до релевантных AC41/43.
Все эти критерии — задачи для будущей реализации, не уже пройденные проверки.

## 14. Текущий статус реализации в ветке

В рабочей ветке уже реализован вертикальный срез:

| Область                  | Что есть                                                                                              | Что остаётся                                                           |
| ------------------------ | ----------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Персональная авторизация | Пользователь подключает свой Atlassian API token; токен шифруется, не возвращается в API              | OAuth authorization-code/refresh flow и production provisioning        |
| Связь задачи             | Link/unlink существующего Jira issue, защита от дублей и скрытие metadata без личного grant           | Live canary на выбранном tenant                                        |
| Создание Jira            | Английский Jackrabbit draft, проверка, approval, идемпотентная операция, readback и `outcome_unknown` | Live Jira schema/custom fields canary                                  |
| Статусы                  | Policy, mappings, consent, outbound transition task и inbound polling command                         | Подключить worker/beat к production среде и проверить live transitions |
| Ветки/PR                 | Typed development plan с реальным Jira key, branch/PR templates и repository profile                  | Исполнитель branch/PR и readback через существующий Jackrabbit adapter |
| UI                       | Project settings Jira, персональное подключение, Jira card на issue, consent и development plan       | Финальная visual QA на реальном tenant и production feature flags      |

Временная API-аутентификация через личный API token является осознанным
первым срезом, а не заменой OAuth. API token не является общим сервисным
аккаунтом: он принадлежит конкретному Plane user и может быть отозван им.
Для production следует либо явно утвердить этот режим, либо включить OAuth
и не считать OAuth acceptance уже пройденным.

Проверено в этой ветке: `manage.py check`, отсутствие неприменённых миграций,
18 backend-тестов (unit + contract), `pnpm check:types`, production build
frontend и Playwright-сценарий: открыть настройки Jira, сохранить policy,
открыть issue, открыть формы Link/Create и получить draft. В base Plane shell
во время локального статического preview наблюдаются существующие 404
`intake-state` и React hydration warning; они не относятся к Jira endpoints.
Живые Jira/GitHub credentials не использовались.
