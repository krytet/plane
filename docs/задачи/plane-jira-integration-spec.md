# Plane ↔ Jira и соглашения разработки Jackrabbit

Статус: проект спецификации для согласования, не разрешение на внешние записи.
Дата: 2026-11-03. Обследование: локальный код и инструкции, read-only.
Реализация, проверка живой Jira-схемы и подключение учётных данных не выполнены.

Дополнение для передачи разработчикам:
[план разработки, персональная авторизация, UI и тестовая матрица](plane-jira-development-handoff.md).
Персональная авторизация ниже заменяет первоначальное предложение общего service account.
Обязательные уточнения после review находятся в разделе 13 handoff:
раздельное авторство/полномочия фоновых действий, инвалидация consent,
матрица ролей, takeover и setup readiness; критерии AC21–AC46 дополняют AC01–AC20.
Текущий рабочий срез использует личный Atlassian API token как временный способ
персональной авторизации; OAuth остаётся отдельным production release gate
и не считается уже реализованным. Подробности — раздел 14 handoff.

## 1. Проблема и цель

Локальная задача Plane должна при необходимости представлять ту же работу в Jira:
привязка существующей задачи либо создание новой из английского предпросмотра.
Синхронизация состояний включается отдельно. Связанный Jira-ключ определяет
имена новых веток и заголовки PR в настроенном репозитории.

Цель: не заставлять пользователя вручную поддерживать две копии статуса,
не теряя ручные изменения, не создавая дубликаты и не закрывая задачу до её
фактического принятия. Локальные задачи без Jira продолжают работать как раньше.

## 2. Критерии приёмки и способ проверки

Все строки ниже — предлагаемый контракт реализации. Продуктовые решения
из раздела 16 должны быть подтверждены до включения внешних записей.

| ID   | Критерий                                                                                         | Проверка                                                                      |
| ---- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| AC01 | Пользователь видит только разрешённые workspace/project/connection                               | API-тесты пересечения двух workspace, пользователей и Jira-sites              |
| AC02 | Привязка существующего тикета ничего не изменяет в Jira и не синхронизирует статус автоматически | Контрактный тест: GET разрешён, внешних POST нет                              |
| AC03 | Параллельные create/link не создают две активные связи                                           | Транзакционный тест двух запросов и уникальных ограничений                    |
| AC04 | Jira создаётся только по подтверждённому английскому снимку                                      | Изменение исходного текста или полей после preview аннулирует подтверждение   |
| AC05 | Обязательные поля и переходы проверяются для выбранных project/type/issue                        | Fixtures с различными схемами и изменение схемы между preview и submit        |
| AC06 | Повтор запроса с тем же ключом возвращает ту же операцию                                         | Повтор после client timeout и рестарта worker                                 |
| AC07 | Неизвестный результат внешнего create не вызывает слепой повтор                                  | Потеря ответа после успешного Jira POST; один тикет, операция требует сверки  |
| AC08 | Синхронизация выключена по умолчанию; включение не переносит старый backlog                      | Тест effective_at, preview первоначального согласования                       |
| AC09 | Повторы, обратные уведомления и перестановка событий не образуют цикл                            | Повторная доставка и out-of-order property/sequence tests                     |
| AC10 | Конкурирующие ручные изменения видны как конфликт                                                | Два изменения после общей базы; ни одна сторона не перезаписана автоматически |
| AC11 | LLM Complete не закрывает Jira и не выставляется входящим Jira-событием                          | Регрессия существующего финализатора и chat workflow                          |
| AC12 | Done и Cancelled различаются; reopen требует отдельного разрешения                               | Матрица терминальных переходов, resolution и hold                             |
| AC13 | Отключение/отвязка запрещает ещё не отправленные записи                                          | Disable во время очереди, повторная проверка policy version перед dispatch    |
| AC14 | 401/403/404/429/5xx отражаются правдиво, без потери локальных правок                             | Provider fault tests и UI ошибок с correlation ID                             |
| AC15 | Ветка/PR используют реальный ключ и выбранный repository profile                                 | Имена, коллизии, иной Jira project, другой base/head repository               |
| AC16 | Создание PR не означает merge, deploy или закрытие Jira                                          | E2E: открытый, закрытый без merge, merged, reopened PR                        |
| AC17 | Секреты отсутствуют в API-ответах, logs, audit, prompts и артефактах                             | Redaction tests; snapshot проверки сериализаторов                             |
| AC18 | После рестарта локального компьютера синхронизация восстанавливается без лавины запросов         | Durable queue, ограниченное догоняющее чтение и свежесть UI                   |
| AC19 | Существующие интеграции, импорт и chat workflow не изменяют поведение                            | Имеющиеся unit/contract tests плюс целевые регрессии                          |
| AC20 | Фактический успех подтверждается чтением внешнего объекта                                        | Create/transition/PR readback; несоответствие остаётся unverified             |

## 3. Границы

В поставку входят: подключение, metadata/schema discovery, link/unlink,
английский черновик и создание, четыре режима status sync, конфликты,
условное автозакрытие, профиль имён ветки/PR, интеграционный журнал и recovery.

Не входят: массовый импорт всей Jira, универсальный редактор workflow,
синхронизация комментариев/вложений/оценок/спринтов/ранга, двустороннее
перезаписывание описаний, автоматические merge/deploy, перенос всех старых веток,
многозадачные PR как автоматический механизм закрытия, Jira Data Center.
Публичный webhook relay не требуется для первой версии.

Для существующей ветки допускается привязка и диагностика соответствия;
переименование требует отдельного действия. Unlink никогда не удаляет Jira.

## 4. Проверенная база и решения

### 4.1. Что подтверждено локально

| Источник                                                                          | Наблюдение                                                                                                  | Следствие                                                                                |
| --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `apps/web/services/integrations/jira.service.ts`                                  | JiraImporterService вызывает importer endpoints                                                             | Не считать импорт готовой постоянной синхронизацией                                      |
| `apps/api/plane/db/models/integration/base.py`                                    | Есть Integration и WorkspaceIntegration                                                                     | Использовать существующий integration shell, не копировать хранение секретов без анализа |
| `apps/api/plane/db/models/integration/github.py`                                  | Есть GitHub repository/issue/comment sync models                                                            | Это не доказательство готового создания branches/PR                                      |
| Поиск `jira` в Python `apps/api/plane`                                            | Найдены importer/provider enum и OpenAPI описание                                                           | Полноценный Jira sync service в обследованном коде не обнаружен                          |
| `docs/issue-chat-workflow.md`                                                     | Versioned policy, cursor, audit; защищённые состояния; отдельная completion boundary                        | Jira writer обязан согласовываться с существующей state machine                          |
| `apps/api/plane/db/models/issue.py`                                               | State change связан с completed_at; есть chat policy/cursor/audit                                           | Нельзя обновлять state прямым SQL или обходить side effects                              |
| `D:/ubuntu/jackrabbit3/.agents/skills/git-workflow/SKILL.md`                      | Ветка `FP-XXXXX-brief-description`; PR `FP-XXXXX: Brief description`; normal branch от master, PR в staging | Профиль Jackrabbit, а не глобальные defaults Plane                                       |
| `D:/ubuntu/jackrabbit3/.agents/skills/jira-ticket/references/ticket-templates.md` | Short summary, Context, Full description обязательны                                                        | Версионировать этот шаблон                                                               |
| `D:/ubuntu/jackrabbit3/.agents/skills/jira-ticket/SKILL.md`                       | English Description; отдельный bilingual путь с Russian Developer Details                                   | Не смешивать русский текст с description; выбор custom field profile согласовать         |

История просмотренных частей: `acc91a883` и `8f32201ba` относятся к board workflow
и verified completion; это основание сохранить контракт, а не заменить его Jira-автоматикой.
Живая Jira, фактические права и актуальность локальных Jackrabbit-правил на сервере не проверены.

### 4.2. Предлагаемые решения

1. Источник идентичности Jira — site ID + issue ID. Ключ и URL — обновляемые атрибуты.
2. Один активный Jira binding на Plane issue; один Jira issue на одну активную Plane
   задачу внутри workspace. Между workspace возможны изолированные связи.
3. Site registration принадлежит workspace, личный grant — пользователю в workspace,
   routing policy — проекту, override и automation consent — конкретной связи задачи.
4. Link и sync — независимые функции. Начальный режим `off`.
5. Для локального развёртывания основа — исходящий polling и durable outbox.
   Webhook в будущем только ускоряет обнаружение, не является источником доверенного состояния.
6. Английский текст создаётся из разрешённого снимка Plane, а не из полного чата.
   LLM готовит черновик, но не получает полномочий внешней записи.
7. Продуктовый backend использует отдельный персональный grant пользователя;
   подключение Atlassian в чате не является автоматически доступным credential для Plane.
   Jackrabbit запрещает агентам обходить connector для внешних записей: разрешение
   на развёртывание нового постоянного product adapter необходимо подтвердить отдельно.
   Общий service account не используется. Ручные операции выполняются от имени
   инициатора; фоновые — только по explicit consent владельца автоматизации,
   без заимствования чужого grant. Подробности в development handoff.
8. Git/PR сначала используют общий именующий контракт и существующий workflow
   репозитория. Новый универсальный runner shell-команд из Plane не создаём.

## 5. Пользовательские сценарии

| ID   | Действие                                                       | Результат / исключение                                                              |
| ---- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| UC01 | Пользователь подключает свою Jira в разрешённом workspace site | Проверка account/capabilities; grant доступен только владельцу                      |
| UC02 | Выбирает project, board, issue type                            | Показываем поля, состояния, доступность и ограничения                               |
| UC03 | Вставляет URL/ключ существующей Jira                           | Preview точного объекта; подтверждение создаёт локальную связь                      |
| UC04 | Jira уже связана с другой локальной задачей                    | Показываем доступную пользователю связь; merge/rebind не делаем скрыто              |
| UC05 | Нажимает Create in Jira                                        | Английский draft, недостающие поля, редактирование, confirm                         |
| UC06 | Исходная Plane-задача изменилась во время draft                | Draft stale; regenerate или явное новое подтверждение новой версии                  |
| UC07 | Создание прошло, но Jira не видна на board                     | Успех создания отдельно от board visibility; диагностика фильтра                    |
| UC08 | Включает синхронизацию                                         | Preview mapping и текущего расхождения; выбор baseline без скрытой записи           |
| UC09 | Меняет статус с разрешённым направлением                       | Local commit немедленно, внешняя операция queued → verified                         |
| UC10 | Меняет статус при off/другом направлении                       | Изменение остаётся локальным; показываем расхождение                                |
| UC11 | Обе стороны изменены                                           | Conflict; выбор актуального target после fresh read, audit решения                  |
| UC12 | Закрывает Jira и хочет закрыть Plane                           | Только разрешённое входящее правило; hold блокирует auto-apply                      |
| UC13 | Переводит Plane в Done                                         | Только разрешённое исходящее правило; transition/required fields/readback           |
| UC14 | Возобновляет завершённую работу                                | Reopen выключен по умолчанию; явный preview перехода                                |
| UC15 | Приостанавливает/отвязывает интеграцию                         | Новые записи остановлены, история и удалённые задачи сохранены                      |
| UC16 | Создаёт ветку для связанной задачи                             | Получает валидный plan: repo, base, Jira key, branch name                           |
| UC17 | Готовит PR                                                     | Проверка binding, head/base, gate workflow, title/body и human confirmation         |
| UC18 | Ветка/PR уже существуют                                        | Возвращаем точный совпавший объект или показываем конфликт                          |
| UC19 | Несколько репозиториев/PR относятся к задаче                   | Выбор repository явный; один merged PR не закрывает всю задачу                      |
| UC20 | Компьютер offline/credential истёк                             | Plane работает; очередь и stale индикатор сохранены, writes paused при auth failure |

## 6. Экраны и состояния интерфейса

1. Profile → Integrations → Jira: личное подключение, account, scopes, test,
   reconnect/disconnect. Workspace → Integrations → Jira: разрешённый site,
   policy и health без доступа к токенам других пользователей.
2. Project → Jira: project/board/type, template version, custom field mapping,
   direction, status mapping, closure/reopen, repo profiles, dry-run preview.
3. Issue → Jira card: key/link, observed status и время наблюдения,
   effective sync mode, source of override, last operation, errors.
4. Link modal: input → найденный issue/project/title → policy → confirm.
5. Create modal: English Summary/Description, dynamic required fields,
   исходная версия, warnings, approve snapshot, async receipt.
6. Conflict panel: baseline/local/remote, причины, fresh preview решения.
7. Development card: repo, branches, PRs и реальные состояния; отдельные
   действия Prepare branch / Prepare PR / Execute approved action.
8. Operations view: queued/running/unknown/blocked/verified; retry только
   для допустимых классов, diagnostics без секретов.

Не показывать зелёный «синхронизировано» по факту dispatch.
Отсутствие свежих данных показывается как stale, а не как актуальный статус.
Все действия доступны с клавиатуры; pending состояния блокируют повторный submit;
ошибки доступны screen reader; frontend не является границей авторизации.

## 7. Модель данных и ограничения

Имена ниже проектные: финальное размещение следует conventions репозитория.

| Entity               | Основные поля                                                                                                                        | Инвариант                                                                  |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| JiraSiteRegistration | workspace, site_id, base_url, enabled                                                                                                | Общая конфигурация без пользовательского токена                            |
| JiraUserGrant        | site registration, user, Jira account ID, encrypted auth_ref, scopes, version, health                                                | Grant только владельца; unique active workspace/user/site                  |
| JiraProjectPolicy    | site registration, Plane project, Jira project ID, board ID, type ID, direction, version, effective_at, close/reopen policy          | Один active routing profile на Plane project в v1                          |
| JiraStateMapping     | policy version, issue type, direction, source state ID, target state ID, transition hints, required field defaults                   | Не сопоставлять по имени или board column                                  |
| JiraIssueBinding     | issue, site, Jira issue ID/key, active, generation, override, last observations, automation owner/consent                            | Уникальность активных связей; immutable history; grant не становится общим |
| JiraDraft            | issue revision/hash, policy/template/schema versions, summary, ADF, fields, approval hash, actor                                     | Любое изменение payload инвалидирует approval                              |
| IntegrationOperation | UUID, idempotency key, binding generation, policy version, payload hash, status, attempts, lease, next_attempt, correlation, receipt | Unique scope+key; key с другим hash → conflict                             |
| IntegrationOutbox    | event ID, issue revision, origin, causation ID, payload, dispatched_at                                                               | Запись в транзакции локального изменения                                   |
| JiraSyncCursor       | baseline local revision/remote fingerprint, expected target, last verified observation, poll checkpoint                              | Не использовать часы разных систем как общий порядок                       |
| IntegrationAudit     | actor, action, local/remote before-after, reason, operation ID                                                                       | Append-only; фильтрация по доступу                                         |
| RepositoryProfile    | repository ID, clone/base/PR target refs, naming templates, workflow adapter                                                         | branch base и PR base отдельны                                             |
| DevelopmentLink      | issue binding, repository ID, branch ref, PR ID/number, head SHA, lifecycle                                                          | Несколько links допустимы; точная identity                                 |

DB partial unique constraints учитывают soft delete. Unlink меняет generation,
чтобы старые jobs не могли работать на новой связи. Archive/disconnect не стирают audit.
Где возможно, переиспользовать Integration/WorkspaceIntegration; не расширять
старый GithubIssueSync до PR-сущности с иной семантикой.

## 8. Backend и API-контракты

Предлагаемые маршруты относительно существующего workspace/project API:

| Endpoint group                | Операции                                   | Контракт                                                               |
| ----------------------------- | ------------------------------------------ | ---------------------------------------------------------------------- |
| `me/integrations/jira`        | connect/callback/test/reconnect/disconnect | Только собственный grant; callback привязан к пользователю и workspace |
| `integrations/jira/sites`     | register/pause                             | Admin-only; без выдачи чужих grants                                    |
| `projects/{p}/jira-policy`    | GET/PUT/preview                            | optimistic version; проверка всех ссылок в том же workspace            |
| `jira-metadata`               | projects/boards/types/fields/statuses      | Пагинация; cache по site/project/type/principal                        |
| `issues/{i}/jira-binding`     | GET/POST/DELETE                            | POST link по проверенному ID; DELETE локально                          |
| `issues/{i}/jira-drafts`      | create/revise/validate/approve             | Нет Jira writes; return versions/hash/errors                           |
| `jira-drafts/{d}/submit`      | POST                                       | Idempotency-Key + approved hash; 202 + operation ID                    |
| `issues/{i}/jira-sync`        | preview/resolve/reconcile                  | Remote mutation только явной разрешённой операцией                     |
| `issues/{i}/development`      | plan/attach/execute                        | План не выполняет Git; executor принимает только typed fields          |
| `integration-operations/{id}` | GET/retry                                  | Retry не создаёт новый logical intent                                  |

Различать 400 validation, 403 local permission, 409 stale/conflict,
422 unsupported mapping/fields, 202 accepted. Provider errors возвращать
структурированным error_code, retryable, sanitized message, correlation_id.
Не выдавать необработанные ответы провайдера клиенту.

Разделить адаптеры: JiraClient (transport), MetadataService, DraftService,
BindingService, SyncPlanner (чистые решения), SyncExecutor, Reconciler,
NamingPolicy и DevelopmentAdapter. Не смешивать генерацию текста с dispatch.

## 9. Синхронизация и устойчивость

### 9.1. Режимы

`off`, `plane_to_jira`, `jira_to_plane`, `bidirectional`.
Override задачи: `inherit` либо один из четырёх режимов.
Close и reopen — независимые разрешения, не неявное следствие bidirectional.
Изменение policy увеличивает version и аннулирует несовместимые pending jobs.

Для первоначального link/enable сохраняем наблюдаемую пару состояний.
Несовпадение требует выбора: оставить baseline без записи, применить Plane
к Jira или Jira к Plane с предпросмотром. Исторические события не воспроизводим.

### 9.2. Исходящая запись

1. Изменение Plane state и outbox фиксируются одной DB-транзакцией.
2. Worker арендует operation; короткие DB locks, без сети внутри транзакции.
3. Перед отправкой проверяет active binding generation, policy version, permission,
   текущую local revision, protected state и свежий Jira state.
4. SyncPlanner решает no-op / transition / conflict / blocked.
5. Исполнитель получает разрешённый transition для текущего Jira issue,
   валидирует required fields, выполняет его и читает результат обратно.
6. Cursor и audit фиксируют verified результат; ожидание ответа не равно успеху.

Внешняя система не участвует в общей DB-транзакции. Lease сам по себе не
гарантирует exactly-once: после истечения lease dispatched операция сначала
сверяется, а не отправляется другим worker заново.
Пауза не может отменить уже отправленный HTTP request: UI сообщает in-flight;
по завершении допустим только readback, не автоматическая обратная запись.

### 9.3. Входящее чтение и конфликты

Для v1 polling только активных bindings; pagination, checkpoints, ограничение
параллелизма по connection. Предлагаемый интервал 60 секунд с jitter,
metadata TTL 15 минут; это конфиг, а не обещанная SLA. Catch-up после сна
не запускает job на каждый пропущенный интервал.

Webhook, если будет отдельно разрешён: проверка подлинности выбранного
transport, размер/тип payload, dedup, затем fresh GET Jira.
Не открывать локальный сервер наружу автоматически.

База сравнения — last verified pair. Если изменилась одна сторона, возможно
разрешённое применение; если обе стороны изменились различно — conflict.
Если обе уже соответствуют mapping, no-op и новая база.
В одностороннем режиме ручное изменение другой стороны не перетирается
на каждом poll: drift виден, новое reconciliation требует preview.

Многие локальные states могут отображаться в один Jira state. Обратное
отображение обязано иметь один явно выбранный canonical target; обратный
echo собственной записи не должен менять LLM Work на обычный In Progress.
origin/causation используются вместе с expected state/readback, не вместо них.

Удалённые гонки между GET и transition полностью исключить нельзя:
post-readback mismatch становится conflict; не выполнять компенсационные
переходы автоматически и не обещать атомарность между системами.

### 9.4. Совместимость с chat workflow

Все Plane state writers должны соблюдать единый порядок блокировок и
completion/manual-hold boundary существующего workflow. Перед implementation
инвентаризировать REST, bulk, import, jobs, chat и finalizer write paths.
Общий state-change service должен сохранять completed_at, activity, realtime
и outbox в согласованной транзакции. Reconciler страхует пропущенные события,
но не заменяет надёжную запись outbox.

Нельзя выставлять LLM Complete из Jira. Нельзя считать LLM Complete эквивалентом
Done. Входящий переход из LLM Complete в настоящий Done разрешается только
явным closure policy и при отсутствии нового активного хода; иначе конфликт.
Manual hold блокирует входящие auto transitions. Новая работа в чате
не должна повторно открывать закрытую Jira без разрешения на reopen.

## 10. Создание английского тикета

1. Snapshot включает только выбранные title, description и разрешённые ссылки.
   Chat transcripts, скрытые поля и вложения не отправляются автоматически.
2. Пользователь выбирает Task/Bug/Story из реальных типов Jira.
   Jackrabbit profile рекомендует Task; Bug требует Actual/Expected;
   Story — подтверждённого Major scope. Не угадывать соответствующие type IDs.
3. Шаблон содержит `Short summary`, `Context`, `Full description`.
   Context объясняет actor/workflow, purpose, термины, baseline/boundary,
   starting points. Summary не повторяется отдельным заголовком description.
4. Дополнительные секции только с подтверждёнными данными. Никаких выдуманных
   owners, сроков, root cause, metrics, selectors или обещанной проверки.
5. Description на английском; ADF валидируется и санитизируется.
   Cyrillic validator — проверка явного нарушения, не доказательство качества перевода.
   Для исходных идентификаторов с кириллицей нужен явный reviewed exception,
   а не скрытая транслитерация идентификаторов.
6. Для English-only profile используются правила legacy template без обязательного
   Russian Developer Details. Bilingual profile — отдельная опция; его custom field
   IDs/options проверяются live и никогда не считаются универсальными.
7. Не публиковать Markdown как ADF без преобразования. Диаграммы не генерировать
   по умолчанию; при включении требовать проверяемый readable preview.
8. Missing required fields показываются до submit. Неподдерживаемый field schema
   блокирует submit с указанием поля; не заменять произвольным JSON редактором.
9. Submit закрепляет operation UUID, payload hash и approval. Provider create
   сопровождается readback issue ID, summary, description и выбранных полей.
10. Идентификатор операции по возможности записывается в поддерживаемую Jira
    property вместе с create; способ поиска проверяется на выбранном tenant.
    Не предполагать, что arbitrary property доступна в JQL.
11. Если ответ create потерян: статус unknown, bounded reconciliation кандидатов
    по проверяемому marker. Нулевой результат ещё не доказывает отсутствие задачи.
    Без однозначного доказательства новый POST запрещён; UI позволяет прикрепить
    найденный issue после проверки или передать оператору.
12. Локальный link сохраняется после обнаруженного успешного create. Ошибка
    сохранения link лечится тем же operation receipt, а не созданием новой Jira.

Ручной английский draft остаётся доступен без LLM. Выбор LLM provider и разрешение
передачи данных — отдельная конфигурация; никакие ключи из окружения агента
не копируются в приложение.

## 11. Ветки и pull requests

Профиль Jackrabbit по локальным правилам:

| Поле                             | Шаблон                                                                     |
| -------------------------------- | -------------------------------------------------------------------------- |
| branch                           | `{jira_key}-{english_slug}`                                                |
| PR title                         | `{jira_key}: {english_summary}`                                            |
| commit subject, если формируется | `{jira_key}: {summary}`                                                    |
| normal branch source             | `master`                                                                   |
| normal PR target                 | `staging`                                                                  |
| PR body                          | Структура Jackrabbit; проза на русском, точные идентификаторы без перевода |

Пример только формата: `FP-12345-fix-label-validation`.
Если реальный Jira project имеет ключ FS, результат будет `FS-12345-...`;
подменять FP на FS нельзя. Lowercase применяется к slug, ключ сохраняется как Jira key.
PR title language предлагается English; пользователь пока явно подтвердил
английский именно для Jira description.

NamePolicy: allowlist токенов, ограничение длины, корректный git ref,
без shell interpolation; deterministic slug + при необходимости согласованный
suffix для коллизии. Не добавлять agent credit. Git identity берётся из пользовательской
конфигурации; missing identity блокирует commit.

Первая поставка предоставляет immutable DevelopmentPlan с repo ID, issue ID,
Jira key, branch source SHA/ref, head ref, PR base, title, body, plan hash.
Существующий Jackrabbit executor потребляет этот plan и выполняет свои
worktree/review/CI gates. Наличие plan не означает разрешение push или PR create.

Без зарегистрированного executor UI предлагает copy plan и attach existing PR,
но не заявляет, что создание реализовано. Для полного AC15 нужно реализовать
и проверить adapter к фактическому entry point создания веток/PR.
Выбор T3/CLI/Plane executor — release gate, не скрытая деталь.

При создании: validate repo доступ, actual base/head, Jira binding freshness,
required existing workflow gates; confirmation; execute; readback PR ID/title/head/base.
Для repeated create искать по точным repo/base/head, а не одному title.
PR может быть несколько; один primary выбирается явно.
Closed without merge не равно Done. Merge не равно deployment.
Переименование Jira key не переименовывает published branch автоматически;
новые имена используют актуальный key, старые links сохраняют stable issue ID.

## 12. Пограничные случаи и реакция

| Случай                                      | Обязательная реакция                                                                |
| ------------------------------------------- | ----------------------------------------------------------------------------------- |
| Jira URL другого домена / credentials в URL | Reject до сети; allowlisted site, SSRF/redirect защита                              |
| Board охватывает несколько проектов         | Explicit target project; board не определяет create project                         |
| Board filter исключает созданный issue      | Тикет успешно создан; visibility warning, без изменения фильтра                     |
| Нет Browse/Create/Transition permission     | Показать недоступную capability; не маскировать 403 ретраями                        |
| 404                                         | Unavailable: удаление и скрытый доступ не различать без доказательства              |
| Jira move/key rename                        | Refresh по stable ID; новый project требует revalidation policy                     |
| Удалён Plane state/Jira status/type         | Mapping invalid; pause затронутых операций                                          |
| Transition отсутствует / требует поля       | Blocked с доступными вариантами, без обходных multi-hop переходов                   |
| Разные workflows у типов задач              | Mapping и validation по type/current issue                                          |
| Jira automation меняет результат transition | Fresh read, conflict; не запускать ping-pong                                        |
| Done и Cancelled имеют похожие названия     | Семантика задаётся IDs и policy; resolution проверяется отдельно                    |
| Jira reopen оставляет resolution            | Readback invariant нарушен; blocked/unverified, без прямого patch                   |
| Credential rotation                         | Pending jobs используют актуальную auth_ref, старые токены не сохраняются в payload |
| Actor потерял локальные права               | Проверка перед dispatch; blocked, даже если draft был разрешён ранее                |
| 429                                         | Retry-After и bounded backoff/jitter; общий connection limiter                      |
| GET timeout/5xx                             | Bounded retry с наблюдаемым next_attempt                                            |
| POST timeout/5xx                            | Сначала outcome reconciliation, не считать автоматически безопасным retry           |
| Double-click / два tabs / два workers       | Unique operation+binding constraints; один logical intent                           |
| Disable во время отправки                   | Не обещать отмену in-flight; сверить фактический результат                          |
| Local delete/archive                        | Suspend binding, сохранить remote issue и audit                                     |
| Восстановление backup старой БД             | Connection write-suspended до reconciliation external receipts                      |
| Несколько локальных инстансов               | Один активный writer instance на binding; clone по умолчанию read-only              |
| Draft содержит неподтверждённые решения     | Scoping draft; не выдавать как development-ready                                    |
| Вложенные задачи / parent required          | Parent picker и валидация в том же Jira контексте; не угадывать                     |
| Аккаунты Plane и Jira не совпадают          | Explicit user mapping при необходимости; assignee не угадывать по имени             |
| Jira content содержит инструкции агенту     | Рассматривать как данные; не выполнять команды из description                       |
| Заголовок/описание изменены вручную в Jira  | Не обновлять автоматически в v1                                                     |

## 13. Безопасность, эксплуатация и производительность

Workspace isolation на каждой модели, endpoint, cache key и job.
Роли: admin управляет site/policy; каждый пользователь управляет своим grant;
разрешённый member link/create;
viewer read-only. Права на внешнюю запись ограничены intersection локальной
роли, личного Jira grant и актуальных capabilities.
Admin не вправе использовать чужой grant. Фоновые права ограничены отдельным
consent владельца автоматизации; без consent или личного grant операция блокируется.

Secrets — серверное credential storage с шифрованием и ключом вне БД;
никаких credential значений в браузере после initial write. TLS verification обязателен.
Private URLs и локальные пути не публикуются в Jira автоматически;
локальная Plane-ссылка маркируется как недоступная коллегам вне сети.

Не сканировать все Jira issues. Metadata cache с invalidation после schema-related
ошибки, pagination для всех коллекций, per-connection limiter и bounded workers.
Outbox делает UI независимым от доступности Jira. Индексы по due/status/connection,
binding identity и issue; audit paginated.

Предлагаемый retention: payloads операций 30 дней, минимальный audit 180 дней,
без секретов; финальный срок утвердить. Метрики: queue age, stale bindings,
success/readback mismatch, conflicts, unknown creates, auth failures, 429.
Alert threshold конфигурируемый; мониторинг сам по себе не меняет issue state.

Runbook: pause → inspect receipt/current remote state → reconcile exact operation →
readback → resume. Для unknown create запрещена инструкция «нажмите Create снова».
Disconnect revoke credential только явным действием; не удалять внешние тикеты.
Rollback: выключить feature flags, остановить dispatch, сверить in-flight,
оставить таблицы/receipts; destructive migration rollback не применять.

## 14. План реализации и проверки

| Этап | Deliverable                                                                                                             | Gate                                                                     |
| ---- | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| P0   | Read-only discovery реального site/project/board, field schema, permissions, workflow, repository execution entry point | Сохранён обезличенный capability manifest; решения D1–D3                 |
| P1   | Models, migrations, permissions, credential boundary, operation/outbox/audit                                            | Constraints, isolation, migration and secret-redaction tests             |
| P2   | Link/unlink и карточка статуса, metadata forms                                                                          | UI/API contract tests; никакой внешней записи                            |
| P3   | English draft, schema validator, confirm/create/readback, unknown recovery                                              | Fault injection на каждом crash boundary; sandbox canary                 |
| P4   | State planner, shared writer integration, outbound/inbound opt-in                                                       | Existing chat/finalizer regression, races, disabled and protected states |
| P5   | Bidirectional conflict UI, close/reopen controls, polling recovery                                                      | Два writers, moved issues, stale schemas, offline/restart                |
| P6   | Naming policy, DevelopmentPlan, выбранный execution adapter, branch/PR receipt                                          | Exact repository workflow, collisions, retries, no auto merge            |
| P7   | Admin diagnostics, runbook, feature flags, staged rollout                                                               | Полная матрица AC и приёмка end-to-end                                   |

Можно реализовывать P2 UI параллельно чистому SyncPlanner, но не несколько
конкурирующих изменений одного state writer без согласованного контракта.
Не объединять первый рабочий link с включением всех sync writes.

Тесты: unit для planner/template/naming; DB transaction tests для constraints,
locks и outbox; provider contract fixtures для pagination/ADF/transitions;
fault injection для network response loss, crash before/after local commit;
UI tests для permissions, keyboard, pending/conflict/stale; opt-in sandbox E2E.
Mocks не доказывают доступность реальной Jira или GitHub.

Запускать backend по `apps/api/tests/RUNNING_TESTS.md` и project AGENTS,
frontend format/lint/types и релевантные тесты. В отчёте различать passed,
failed, skipped и непроверенные live gates. Никаких production writes тестами.

Контрольный E2E после отдельного разрешения: создать один тестовый Plane issue,
подготовить/подтвердить Jira draft, получить один remote issue и readback,
включить одну пару переходов, проверить обе стороны и отключение,
подготовить и создать тестовую ветку/PR через разрешённый adapter.
Удаление/закрытие тестовых внешних объектов — отдельный согласованный cleanup.

## 15. Умолчания до согласования

| Настройка                     | Предложение                                                 |
| ----------------------------- | ----------------------------------------------------------- |
| Deployment                    | Local Plane, outbound polling; Jira Cloud adapter           |
| Link cardinality              | 1:1 внутри workspace                                        |
| Sync                          | off; включается явно                                        |
| Initial recommended direction | Plane → Jira                                                |
| Close                         | Только явный согласованный Done; LLM Complete не терминален |
| Reopen                        | off                                                         |
| Description updates           | Только создание; дальнейшие правки вручную                  |
| Jira template                 | English-only с обязательными тремя секциями Jackrabbit      |
| Jira key                      | Реальный key; без жёстко заданного FS/FP                    |
| Git execution                 | Существующий репозиторный workflow через typed plan         |
| PR lifecycle                  | Не закрывает Jira автоматически в v1                        |

## 16. Открытые решения и readiness

| ID  | Что нужно подтвердить                                                                              | Рекомендуемый ответ                                                                     |
| --- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| D1  | Jira site/project/board, Cloud ли это; FS или существующий FP; разрешённый persistent user adapter | Подтвердить точную доску; реальный ключ; личная авторизация, без общего service account |
| D2  | Кто управляет статусом и что считается закрытием                                                   | Plane → Jira по opt-in; ручной Done; обратное закрытие отдельной настройкой             |
| D3  | Где исполняется branch/PR creation; нужен ли Russian Developer Details                             | Existing Jackrabbit workflow adapter; English-only Jira description для v1              |

P0 уточняет технические факты без дополнительных продуктовых вопросов:
actual required fields, auth method/scopes, transition/resolution invariants,
tenant custom fields, repo adapter entry point, существующие write paths.

Статус готовности: можно оценивать и декомпозировать работы по этой спецификации.
Разработку адаптеров и включение внешних writes нельзя считать полностью
разблокированными до D1–D3 и capability manifest. Нельзя обещать «учтены все
возможные случаи»; перечисленные сценарии образуют проверяемую границу поставки.

## 17. Внешние API-источники

Попытка чтения официальной документации 2026-11-03 не вернула содержимого
через web-инструмент; API-возможности в этом документе являются требованиями
к адаптеру, а не подтверждённым capability manifest провайдера.
На P0 необходимо проверить Jira Cloud REST v3 Issues (create metadata,
create/read/transitions), Jira Software Board API (configuration/filter/projects)
и Jira Cloud rate-limiting documentation. Реальные схемы tenant имеют приоритет
над examples из документации. Адреса для последующей проверки:

```text
https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/
https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/
https://developer.atlassian.com/cloud/jira/platform/rate-limiting/
```
