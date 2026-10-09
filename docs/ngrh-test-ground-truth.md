# NGRH resilience tests: expected results

What each test in `deployment/ngrh-tests.json` is expected to show, and why. The expected result on this page
is the one in the spec, and `tests/test_ngrh_testing_spec.py` fails when the two disagree, so when a run
contradicts an expectation, change the spec and this page together and add the run to the log at the end.

How to read a result:

- **PASS**: the run ends `PASSED`. Every success alarm stayed `OK` through the fault, and through the recovery
  window where the template has one.
- **FAIL**: the run ends `FAILED`. A success alarm went to `ALARM`. This is a finding about the sample, not
  about the tool, and the test records it until the sample is changed.
- **UNKNOWN**: nobody has worked out what should happen. The first run decides, and this page is updated with
  what it showed.

A run that ends `ERROR` or `STOPPED` is neither: the test did not finish, and the report says why.

A test can also name **run checks**, which look at what the run did beyond what Resilience Hub reports. A run
Resilience Hub ends `PASSED` is observed **FAIL** when one of its run checks fails: the run did not show what
the test is for. (A test that expects FAIL keeps the `PASSED` it was given, which already contradicts the
expectation.) The report lists each check with its reason.

A run that ends `FAILED` or `PASSED` can also be **INCONCLUSIVE**: when FIS could not inject the fault (an
experiment that ended `failed`, or an `action_failed` event), nothing was done to the sample, so what the run
says about it is nothing. Resilience Hub still ends such a run `FAILED`. The report heads it "INCONCLUSIVE, the
fault did not run", gives the reason, and `make ngrh-test` exits 3 as for any verdict that isn't the expected
one.

## orders-broker-dependency

**Test:** the orders service, dependency validation. For 15 minutes FIS drops orders' traffic to its Amazon MQ
broker in us-east-1. The broker host is read from the broker's own endpoint (`OrdersMqBroker`), so the name
follows each deployment. The success alarms are the orders journeys from us-east-1 (`journey-lcl-orders` and
`journey-global-orders`), which enter through ui. `region-degraded` for us-east-1 stops the run early if any of
that Region's local journeys fails for long enough. The observability alarms are `hop-orders-slow` and
`orders-created-zero`. `hop-checkout-errors` would say more about the blast radius, but it is tagged checkout,
and Resilience Hub takes only alarms it discovered for the service as test sources, which for orders means the
tags orders and shared. It is in the report's evidence instead (the ten hop alarms are), and preflight refuses
a source outside the service's tags before the run starts.

Expected result: PASS

**Why:** orders publishes the order-created event off the request path. `OrdersEventHandler` still reacts to the
commit of the order, but it only queues the event for a pool of two threads (a queue of 100 behind them) and
returns. Checkout's call to orders therefore never waits on the broker, and the orders journeys stay `OK`. A pool
thread that can't open a connection gives up after 2 seconds (`spring.rabbitmq.connection-timeout`). Events that
can't be sent are lost: a failed publish is logged and not retried, and an event that finds the queue full is
dropped with a warning that names the order. Nothing in the sample consumes these events, and the order itself is
saved either way. `orders-created-zero` is counted inside orders, in a listener that stays on the request thread,
so it stays `OK` too.

**What confirms it:** the run ends `PASSED`. The journey alarms, `hop-orders-slow`, `orders-created-zero` and
the hop alarms of ui and checkout all stay `OK` for the whole fault and the recovery window. Orders' log shows
the fault at work: `Could not publish the order-created event for order <id>: SocketTimeoutException: Connect timed
out` from the pool threads, and `Dropped the order-created event ... the publish queue is full` only if more than
about 100 events pile up.

**What would contradict it:** a journey alarm or `hop-orders-slow` goes to `ALARM`. Then something on the request
path still waits for the broker, or the orders tasks still run an image from before this change (check the task
definition's image tag). The report lays the alarms out by hop, but the order in which they fire does not show
where the time went: in the run of 2026-10-07 the ui alarms fired first, orders' next and checkout's last, all after
the fault had already stopped (see Runs).

**Confidence:** a run, and the code and its tests. Run `a54c42b9` of 2026-10-08 cut the broker for the full 15
minutes and ended `PASSED` (see Runs). JUnit tests create orders through the real service while a broker throws
or never answers, and assert that the order commits, returns within 3 seconds and is counted.

**Before the publish was best-effort** the expected result was `FAIL`, and run `bea5d9f4` of 2026-10-07 confirmed
it. The publish ran on the thread answering checkout's call. Once the open connection to the broker was cut, the
next publish had to reconnect, which can block for up to 60 seconds with the client library's defaults. Checkout's
call to orders reached the 3-second Service Connect limit first, checkout returned an error, ui served an error
page inside the canary's 30-second run, and `orders-created-zero` stayed `OK` because the order was saved first.
If the journeys fail again with the broker cut, compare with this: it is the signature of a publish back on the
request path. The run's timeline is under Runs.

## catalog-recovery

**Test:** the catalog service, multi-Region recovery. For 20 minutes FIS drops catalog's traffic to its Aurora
cluster in us-east-1, the writer endpoint and the reader endpoint, both read from the cluster in that Region's
`catalog-db-stack`. The test names the Region Switch plan so Resilience Hub's report can carry its timeline, but
the plan is started by its own alarm triggers, not by the test. The success alarms are the four global journeys
(`journey-global-*` in us-east-1): they enter through the Route 53 name, so they go green again only once traffic
has moved to us-west-2. The observability alarm is `hop-catalog-slow`. There is no stop condition: stopping the
fault cannot help if us-west-2 fails after DNS has moved, and a guard on us-east-1's own journeys would trip on the
brief catalog pause during the switchover and end the run early. The report's evidence also lists the triggers'
inputs, the four `journey-lcl-*` alarms of us-east-1 and the four `journey-rmt-*` alarms of us-west-2, and both
`region-degraded` alarms, so a run that does not fail over shows which condition was not met.

Expected result: PASS

**Why:** the fault touches only catalog's tasks in us-east-1. Catalog can no longer read its database, so the
catalog journey and the home journey, which both call catalog through ui, fail from us-east-1 after two of three
one-minute canary runs. That meets one trigger: `journey-lcl-<journey>` for us-east-1 is red, `journey-rmt-<journey>`
for us-west-2 (its view of us-east-1) is red, and `region-degraded` for us-west-2 is `OK`, because the fault does
not reach it. The plan then scales the six services up in us-west-2 to twice their 24-hour peak in us-east-1,
moves DNS, and switches the catalog database over to us-west-2, in that order. The global journeys recover once
the new DNS answers reach their canaries. Resilience Hub passes the run when all four are back to `OK` within the
10 minutes of the multi-Region objective in the resiliency policy and stay there.

**Run check `deactivate-completed`:** a deactivate of us-east-1 started during the run and ended `completed`.
Without it a run could pass because the fault did not bite, or because a person moved the traffic by hand, and
show nothing about the triggers. An execution that ended `completedWithExceptions` (a step was skipped or failed
and the run went on) or paused does not count.

**What confirms it:** the run ends `PASSED`, the run check passes, and the report's plan execution shows the
deactivate starting a few minutes after the fault, its three steps completed, and ARC's own recovery time under
the objective of 10 minutes. `make ngrh-test` then waits for us-east-1 to be healthy for ten minutes and runs
`make failback REGION=us-east-1`, which the report records.

**What would contradict it:**

- The run ends `FAILED` although the deactivate completed: the global alarms came back after the 10 minutes. The
  budget is tight. The design estimated 6.5 to 11.5 minutes (3 to 4 to detect, a minute to scale up, up to a minute
  for the DNS change, up to a minute and a half for the next canary run, 3 to 4 for the alarms to clear). The one
  plan execution measured so far (an operator-started deactivate of us-east-1 in test2, 2026-10-08, nothing
  failing) took 6 minutes 18 seconds from its start, and two of the estimates were low: scaling up took 65 to 195
  seconds (carts last, because two extra Java tasks take that long to start), not a minute, and the DNS step took
  121 seconds, not up to one, because it waits for Route 53 to show the health check as unhealthy. The switchover
  took 60 seconds and the final application-health step 0.6 seconds, because no global alarm was red. A failover
  that an alarm starts adds the detection (two failed canary minutes out of three, so 2 to 3 minutes), ARC's
  unpublished delay from the alarms to the start of the plan, and the time the global alarms need to turn green once
  DNS has moved, all before the 10 minutes of the objective can be said to hold from the fault. So a `FAILED` from
  the clock is possible even with every step working. The report's timeline shows where the time went. Either the
  alarms or the plan get faster (the scale-up is the largest part; services that start faster, or a higher
  minimum in the standby Region, would shorten it), or the expectation changes to what the run shows.
- The run check fails with no deactivate: no trigger fired. The evidence alarms say which of the three conditions
  was missing. If the plan has no triggers the deployment was made with `AUTOMATIC_FAILOVER=disabled`, and
  preflight refuses the run before it starts.
- The deactivate paused: the switchover could not finish, most likely because the fault also slowed the primary
  database, and the execution waits in `pausedByFailedStep` with traffic already moved. The README's runbook has the
  three ways out, and `make failback` refuses until the execution is resolved.

**Known from other accounts (read-only, 2026-10-08), and so no longer open:**

- **`executionRegion` is the Region the execution targets.** In the demo account (6 plans, 56 distinct executions) and in
  test4 (1 plan, 7), every execution is listed at one endpoint only, the one it ran at. All 46 activates there have
  `executionRegion` equal to that endpoint, and all 10 deactivates have the other Region: a deactivate of us-east-1 is
  held at us-west-2's endpoint with `executionRegion` us-east-1, which is how `StartPlanExecution`'s `targetRegion`
  reads. The endpoint that did not run an execution answers `GetPlanExecution` with `ResourceNotFoundException`. So the
  run check, the choice of which Region `run` fails back and the live preflight read it correctly, and every endpoint has
  to be listed to see all executions (an endpoint that can't be listed hides what ran there, and the report says so).
- **A trigger-started execution is graceful.** Both of test4's (2026-09-28 and 2026-09-30) ran in mode `graceful`. A
  graceful Aurora step can only switch over, so an automatic run cannot lose data. Their comment is the fixed text
  `Execution started by automated trigger` followed by the trigger's description in square brackets, so the report shows
  which of the eight triggers fired. Both were `activate` executions, so which endpoint a trigger-started `deactivate`
  runs at is still unobserved (the code reads the surviving Region's endpoint first and falls back to the other).
- **A deactivate ends `completed`.** 9 of the demo account's 10 did (one was cancelled), all graceful.

**Not known before the first run:**

- Whether Resilience Hub only records the plan named in `regionSwitchPlan` (current documentation) or starts it
  itself (the template's own description says "to execute during the test"). The report lists every execution that
  started during the run with its mode and comment, and a second execution started a moment after the first would
  be this.
- Whether a trigger-started execution ends `completedMonitoringApplicationHealth` or `completed`; both count. A
  manual deactivate ended `completed`.
- Whether `StartTestRun` accepts the five source alarms. The API says only alarms found during a service assessment
  can be test sources. Catalog's assessment of 2026-09-08 was older than all five alarms (created 2026-10-06), so it
  was assessed again on 2026-10-08 (17:28 to 17:43Z, 19 findings, no cost) before any run. For orders an assessment
  was neither needed nor enough to explain a refusal (a mis-tagged alarm was the cause), so whether the first
  assessment's age alone would have refused the alarms was never tested. If the run is refused with "alarms not
  discovered" now, look at the alarms' tags against the service's input sources first (preflight does), not at the
  assessment.

**Confidence:** some for the setup, none for the result. `make ngrh-tests` created the test in test2 on 2026-10-08
at 17:02Z, and Resilience Hub accepted its parameters as written (the plan ARN, both database endpoints, 20 minutes,
the impaired and recovery Regions, the log group, no stop condition) and its five alarm sources. A second reconcile
found nothing to change, the static preflight passes, and the live preflight refuses the test only at check 8,
because test2's plan has no triggers yet; its capacity part ran on the real numbers (the most tasks any service ran
in us-east-1 in 24 hours was 4, so the scale-up asks for at most 8 of 10). Test2's plan was then updated to this branch's
step order, without triggers (2026-10-08 18:23Z, plan version 2 to 3, same ARN): ARC accepted the workflow, the
10-minute objective, the 26 associated alarms and the reports bucket, and its own evaluation passed at both endpoints
with no warnings.

The plan has since run once for real, and so has `make failback`, both by an operator in test2 on 2026-10-08, with no
fault and no trigger. A graceful deactivate of us-east-1 (started 18:41:56Z at us-west-2's endpoint) completed in
6 minutes 18 seconds, ARC's report rated it Good with 0 errors, and the Aurora writer moved to us-west-2 by switchover.
`make failback REGION=us-east-1` then ran from its preflight to its end without a fix (19:04:25 to 19:08:32Z): the
activate took 2 minutes 6 seconds, the capacity reset put six services back to 2 and a minimum of 2 in us-west-2, and
the writer returned to us-east-1 in 1 minute 19 seconds. Afterwards every ECS count, Auto Scaling minimum and maximum,
task definition, health check, cluster state and alarm matched the state taken before, and the plan was unchanged.
Both switchovers made the Region that gave up the writer fail its catalog-backed journeys for about a minute (its
catalog got `connection refused` and reconnected without restarting a task); no alarm fired on the Region itself, and
the three `journey-rmt-*` alarms that test it from outside were in alarm for two minutes after the failover. The
replay of the two days before shows one trigger met: the first `orders-broker-dependency` run on 2026-10-07, which is
explained, and neither switchover met a trigger.

No trigger has been deployed to test2, no run of `catalog-recovery` exists, and no execution has been started by an
alarm, so what is still untested against the real service is: that the eight triggers start the plan, which endpoint
an alarm-started deactivate runs at, how long it takes from the fault, and whether the fault leaves the plan able to
finish. The first live run is the check.

## cart-isolation

**Test:** the cart service, multi-Region isolation. For 30 minutes FIS cuts us-east-1 off from us-west-2: it blocks the
cross-Region routes of the subnets it reaches, the Transit Gateway peering and the cross-Region VPC endpoints, and
pauses replication of the S3 buckets, the DynamoDB global table (carts) and any MemoryDB multi-Region cluster it
reaches. The test gives no `dependencies`, so the template's three actions that drop traffic to a DNS name (SSM, ECS
tasks, EKS pods) should have nothing to drop: the documentation says an API caller supplies the list explicitly, but the
template's own parameter text says it is "auto-populated from dependency discovery if enabled" (each service shows 67
discovered dependencies, mostly unrelated traffic), and the first run settled it: the report's "Dependencies blocked"
section was empty, so an omitted list stays empty. What the fault reaches is decided by Resilience Hub's discovery of the cart
service (resources tagged `service` `cart` or `shared`), and the report lists it under "What the fault reached". The
success alarms are the four local journeys of us-east-1 (`journey-lcl-*`): they go through the Region's own load
balancer and never leave it, so a Region that can serve alone keeps them `OK` for the whole run. The observability
alarm is `hop-carts-errors`. The stop condition is `region-degraded` for us-east-1: FIS ends the fault when one of
those journeys fails, which is the user impact the test would otherwise go on causing for 30 minutes. The report's
evidence also lists `dynamodb-errors` in us-east-1, `region-degraded` in both Regions, the four `journey-rmt-*` alarms
of us-west-2 (its view of us-east-1, which the cut turns red whatever else happens) and the ten hop alarms.

Expected result: FAIL

**First run (2026-10-09, run `2ede1ccb`): `PASSED`, not as expected, because the DynamoDB pause did not run.** FIS ran
one of the template's nine actions, the route cut on three subnets of us-east-1, and skipped the other eight with
"Target resolution returned empty set". One of them was the DynamoDB global table pause that the expected result rests
on: FIS resolved no `aws:dynamodb:global-table` for the cart service. The four local journeys stayed `OK` for the whole
30 minutes, no plan execution started, and both run checks passed. What the route cut alone did was turn all eight
`journey-rmt-*` alarms red, four in each Region, from 18:29:32 to 18:29:47 (about eight minutes after the action began)
until 2 to 8 minutes after it ended. The run is under Runs. The expectation stays `FAIL` for now: the run contradicts
it because the fault did not reach the table, not because the sample behaved differently, and the page and the spec
change together once it is decided how to close that gap (see "What the first run leaves open"). The paragraphs below
are the reasoning for a fault that does reach the table; the outcome they lead to has not been observed.

**Why:** the sample does not keep serving in us-east-1 when its carts table loses the other Regions (inferred; no run has
reached the table yet), because of four facts.

- The table is a multi-Region strong consistency table. `deployment/database/dynamodb/global-table.yml` sets
  `MultiRegionConsistency: STRONG` with a witness Region, and test2's `cartsTable-dev` has replicas in us-east-1 and
  us-west-2 and a witness in us-east-2 (read 2026-10-09).
- FIS's `aws:dynamodb:global-table-pause-replication` on such a table pauses replication between the experiment
  Region and every other Region, and AWS's own post on the subject (AWS Database Blog, 20 May 2026, "Best practices
  for Amazon DynamoDB Global Tables, Part 3: Validating regional resilience with AWS Fault Injection Service") says
  the isolated replica then answers writes and strongly consistent reads with HTTP 500 errors, because cross-Region
  consensus cannot be reached, while eventually consistent reads still work. Those induced errors also count as
  `SystemErrors`, so `dynamodb-errors` fires.
- Carts reads with strong consistency everywhere: `DynamoDBConfiguration` sets the mapper's reads to `CONSISTENT`, and
  `DynamoDBCartService` sets `ConsistentRead` on its query. So every call to carts fails in the isolated Region, not
  only the writes.
- ui asks carts for the cart on every page (`BaseController.populateCommon`), and a carts error fails the page (test2,
  2026-10-05, with one of two carts tasks stalled: home failed about half its runs and the cart and orders journeys
  nearly all). So all four local journeys fail in us-east-1.

The alarms should change in this order: `dynamodb-errors` first (one 60-second period), then `hop-carts-errors` and the
journeys (two failed minutes out of three), then `region-degraded`, which stops the experiment. FIS documents that
replication may go on for up to five minutes after the action begins, so the first errors can come that late. Nothing
should change in us-west-2: the pause isolates us-east-1's replica, and us-west-2's replica and the witness still form
the quorum, so us-west-2 keeps serving and its `region-degraded` stays `OK`. (The blog's example has three replicas;
that a witness in the place of the third replica gives the same result is inferred.)

**What the plan does about it:** the live preflight requires the triggers (check 8), so a journey that fails locally can
start a failover. Each of the four triggers that deactivate us-east-1 needs its journey red in us-east-1, red in
us-west-2's view of us-east-1, and `region-degraded` for us-west-2 `OK`. The first holds, the second holds because of
the cut itself, and the third by the paragraph above, so a trigger should fire and the plan should fail us-east-1 over to
us-west-2 (graceful, with the catalog switchover last). `make ngrh-test` then waits for us-east-1 to be healthy for ten
minutes and runs `make failback`. How the stop condition and the triggers race each other is not known: DynamoDB says
the induced errors continue "briefly" after an experiment ends, which keeps us-east-1's journeys red a little longer,
and the cut ends with the experiment, which turns the `rmt` views green again within minutes.

**Run checks:** `no-plan-execution`, no execution of the plan started during the run or in the ten minutes after it
(the report reads that far), and `destination-stayed-ok`, `region-degraded` in us-west-2 stayed `OK` over the same
window. They decide a run that Resilience Hub passes (a `PASSED` run with a failed check is observed FAIL), and the
report lists them for every run. For a run that is `FAILED` as expected they are information: an execution is the plan
doing its job, and the trigger's description in its comment names the journey that failed locally. For a `PASSED` run an
execution is a finding, because the cut alone should start none.

**What confirms it:**

- The run ends `FAILED` before the 30 minutes are up, with `region-degraded-us-east-1` among the alarms that changed and
  the FIS experiment `stopped`.
- `dynamodb-errors-us-east-1` is the first alarm to change, or moves within a minute of `hop-carts-errors`, and the
  carts table (`AWS::DynamoDB::GlobalTable`) is among what the fault reached.
- `destination-stayed-ok` passes.
- Normally `no-plan-execution` fails by listing one or more deactivates of us-east-1, started by triggers, whose steps
  completed and whose recovery time ARC reports.
- `make ngrh-test` brings us-east-1 back, and the report records the fail-back.

**What would contradict it:**

- The run ends `PASSED`: the fault did not reach the table (the first run, 2026-10-09: it did not), or carts has a way to
  keep serving that the code does not show. Look at "What the fault reached" first, then at
  `FaultInjectionServiceInducedErrors` for `cartsTable-dev` in CloudWatch, which says whether the DynamoDB action
  injected anything.
- The run ends `FAILED` and `dynamodb-errors` never fired, or fired after the journeys: something else broke the Region (the
  routes, the VPC endpoints). The timeline says which alarm moved first.
- The run ends `FAILED` with no execution: the stop condition ended the fault before a trigger's three conditions held
  together. The evidence alarms say which was missing (most likely the `rmt` view of us-east-1, or us-west-2 not green).
  That is a finding about the triggers, not about the sample.
- `destination-stayed-ok` fails: the pause reached us-west-2's replica too, or the failover itself unsettled us-west-2.
- The run is `INCONCLUSIVE`: FIS refused an action. The experiment role carries `dynamodb:InjectError` conditioned on
  `dynamodb:FisActionId`, as `ngrh.yaml` has it; whether the request carries that key is untested.

**Not known before the first run, and what the first run showed:**

- Which resources the fault reaches. **Answered 2026-10-09: three subnets of us-east-1 and nothing else.** The carts
  table is tagged `service=cart` and `ChaosAllowed=true` in both Regions (read 2026-10-09). The cart service's discovery
  (24 resources, read after the assessment) lists three kinds of resource the template's actions act on: six subnets
  (three per Region), one S3 bucket, and the carts table as two `AWS::DynamoDB::Table` entries, one per Region. The
  bucket is the failover plan's reports bucket (it takes the plan stack's `service=shared` tag, so every service
  discovers it, and it has no replication rule). Discovery lists no `AWS::DynamoDB::GlobalTable`, no Transit Gateway and
  no VPC endpoint. Resilience Hub's page for this test says an action with no resource of its target type is skipped,
  and that is what the run showed: the experiment's resolved targets were the three us-east-1 subnets (the template's
  `subnets` target) and none for `dynamodb-global-tables` or `s3-buckets`, and the DynamoDB pause, the S3 pause,
  MemoryDB, the Transit Gateway, the VPC endpoint action and the SSM, ECS and EKS actions were all skipped. The orders
  run had suggested that types are resolved beyond what discovery lists (its task action ran although orders lists no
  `AWS::ECS::Task`); that did not carry over to the DynamoDB pause. The route cut alone did not stop the Region, as
  expected: it serves from local endpoints. The reports bucket was not touched: the S3 pause resolved no bucket (FIS's
  own cross-Region scenario targets only buckets that replicate to the destination, and this one does not replicate;
  that this is the reason was not checked).
- Whether an omitted `dependencies` list stays empty. **Answered 2026-10-09: it stays empty.** The report's
  "Dependencies blocked" section says "none listed", and the SSM, ECS and EKS actions were skipped for lack of targets.
- How long after the action begins the errors start (FIS says up to five minutes) and how long after the stop they go
  on. **Answered for the route cut only:** the cross-Region views failed 7 minutes 42 seconds after the action began
  (route action running 18:21:50, first alarm 18:29:32) and recovered 2 to 8 minutes after it completed (18:51:53):
  us-east-1's four views at 18:53:47 to 18:54:45, us-west-2's at 18:54:32 (home), 18:58:44 (catalog), 18:59:32 (orders)
  and 18:59:42 (cart). For the DynamoDB pause it is still not known, because the pause did not run.
- What ARC does with four triggers that are met in the same minutes: start one execution, start four, or start one and
  refuse the rest. The report lists every execution that started, and `make ngrh-test` fails each deactivated Region
  back once.
- Which endpoint a trigger-started deactivate runs at, and what its comment says (only activates have been seen). The
  first run met no trigger, so both of these are still open.
- Whether Resilience Hub accepts the five source alarms when a run starts. The API takes only alarms found in a
  failure-mode assessment of the service as test sources, and the alarms were created on 2026-10-06. The cart service
  was assessed again on 2026-10-09 (13:35 to 13:50Z), and CloudTrail shows the discovery session asking CloudWatch about
  all five during it (`hop-carts-errors` and the four `journey-lcl-*` alarms of us-east-1). Orders' first run was
  refused until its service had been assessed after its alarms existed, and once more for one alarm tagged outside its
  scope, which the preflight now checks. **Answered 2026-10-09: the assessment was enough.** The run started, Resilience
  Hub evaluated the four success alarms (all `passed`) and the observability alarm, and ended the run `PASSED`.
- Control-plane calls on the table, such as `UpdateTable`, are blocked in every Region while the pause lasts (the
  blog), and FIS allows at most 5,040 minutes of this action per MRSC table in a rolling seven days. Do not deploy the
  table during a run; the limit allows over a hundred 30-minute runs a week. The first run did not pause the table, so
  none of this was exercised.

**What the first run leaves open:** how to make the fault reach the carts table, and what the test is if it cannot.

- The table is an `AWS::DynamoDB::GlobalTable` (logical id `DDBGlobalTable`, physical name `cartsTable-dev`, in the
  stack `carts-db-stack-dev`). The cart service's input source is a tag filter, and discovery by tag lists the table's
  replica in each Region as an `AWS::DynamoDB::Table`. Resilience Hub's service model accepts a CloudFormation stack ARN
  as an input source (`cfnStackArn`, next to `resourceTags` in the same union), which would put the stack itself in the
  service. That discovery then lists an `AWS::DynamoDB::GlobalTable`, and that the pause then takes it as its target,
  are both untested: finding out takes a change to the cart service, a new assessment and a second run. The DynamoDB
  pause is the part that can make the expected result `FAIL`.
- If the table cannot be reached that way, the test is the route cut alone. What the first run showed is then the
  expected result: the Region keeps serving, the eight cross-Region views go red, no trigger is met and no failover
  starts, so the expected result becomes `PASS` and the paragraphs above about the table move to a note on what the
  test cannot cover.

**What it would mean:** a `FAILED` run as expected says that us-east-1 cannot serve on its own while its carts table
cannot reach the quorum, and that the failover is what keeps users served. Reads could continue if carts fell back to an
eventually consistent read when a strongly consistent one fails (the blog suggests this); writes could not, which is
the price of strong consistency, so the cart and orders journeys would still fail locally.

**Confidence:** inferred from AWS's documentation and the repository's code, with the table's mode and the Region's
health read from test2; one run exists (2026-10-09), and it did not reach the table. `make ngrh-tests` created the test
in test2 on 2026-10-09 at 13:35Z, and Resilience Hub accepted its parameters as written (both Regions, 30 minutes, the
log group, the stop condition) and its five alarm sources. It echoed no `dependencies` list, which said that none was
stored, and the run confirmed that none is used. A second reconcile found nothing to change, the static preflight
passed, and the live preflight refused the test only at check 8, because test2's plan had no triggers (with them armed
for the run, it passed). The experiment
role in test2 already has every permission the FIS documentation lists for this action, including
`dynamodb:InjectError` for multi-Region strong consistency tables. That statement is conditioned on
`dynamodb:FisActionId`, a key the DynamoDB service reference does define for `InjectError`; that the request carries it
with this action's name is assumed by analogy with the EC2 example in the FIS documentation, and a run that reaches the
table is what tests it (the first run skipped that action). That ui calls carts on every page is what makes all four
journeys fail rather than only the two that add to a cart.

## Runs

Six attempts so far, on the first deployment this ran on: five of `orders-broker-dependency` and one of
`cart-isolation`. The last two of orders reached a verdict: the first `FAILED`, as then expected, and the last
`PASSED`, as expected now. The one run of `cart-isolation` `PASSED` where `FAIL` is expected, because the fault did not
reach the carts table. `catalog-recovery` has not been run.

- **2026-10-06 and 2026-10-07, `orders-broker-dependency`: refused twice before it started.** `StartTestRun`
  answered "alarms not discovered for this service". A source alarm, `hop-checkout-errors`, is tagged checkout,
  and Resilience Hub discovers only the alarms tagged for the service. A fresh assessment of orders did not
  change that (tried 2026-10-07). The alarm moved to the evidence alarms, and preflight now checks the tags.
- **2026-10-07, `orders-broker-dependency`: the run started and ended `FAILED` after 33 seconds with no fault
  injected.** FIS refused the ECS packet-loss action: "At least one ECS Task is not registered as a SSM managed
  instance". The weekly repave had replaced the sidecar's image with the bare SSM agent, so the sidecar of every
  task started afterwards exited at once (`aws: command not found`) and nothing registered. The first version
  of the report called this the expected FAIL. It is INCONCLUSIVE now, preflight refuses a run whose service has
  a running task that is not registered with SSM, and the repave rebuilds the sidecar image and checks its tools
  before it replaces the one the tasks pull.
- **2026-10-07, `orders-broker-dependency`, run `bea5d9f4`: the first verdict, `FAILED`, after 6 minutes 40
  seconds. It was the result expected then (the publish was still on the request thread), and it is the reason
  the expectation is now `PASS`.** Both orders tasks in us-east-1 were registered with SSM (the sidecar image was rebuilt and the
  services rolled first). Times are UTC:
  - 17:50:02 the packet-loss action started on both tasks, blocking the broker's host name.
  - 17:54:11 the first `Connect timed out` in orders' log, from `OrdersEventHandler.onOrderCreated` through
    `RabbitTemplate.convertAndSend`, after `Created Order` for that request: the publish, on the request thread,
    as inferred. The orders canaries failed from 17:53.
  - 17:55:17 `journey-global-orders` went to `ALARM`; 17:55:56 `journey-lcl-orders` and `region-degraded` for
    us-east-1 did; 17:55:57 FIS halted the experiment on the stop condition; the run ended 17:56:26 with
    "Experiment halted by stop condition."
  - After the fault had stopped, the hop alarms fired: ui 17:57:30 and 17:57:39, orders 17:58:51 and 17:58:54,
    checkout 17:59:40 and 17:59:53. All had cleared by 18:04. The journey alarms were `OK` again at 17:58:17 and
    17:58:56. `orders-created-zero`, carts, catalog and everything in us-west-2 did not change.
  - FIS delivered its log to `/aws/fis/ngrh-tests-dev` (start, target resolution, action and end events), and the
    SSM commands behind the action all ended `Success` or `Cancelled`, none left running.
  - The report written when the run ended missed all the hop alarms, which changed after it. `make ngrh-test` now
    waits ten minutes after the run (`SETTLE_WAIT`) before it writes the report, and a report written earlier says
    that it is early.
- **2026-10-08, `orders-broker-dependency`, run `a54c42b9`: `PASSED`, as expected, after 20 minutes 44 seconds.**
  The same fault on the same two orders tasks as run `bea5d9f4`, with the best-effort publish deployed (orders
  running image tag `a1802e5`, put there by the repave of 2026-10-07). Times are UTC:
  - 11:27:17 the packet-loss action started on both tasks; 11:42:17 it completed, after the full 15 minutes. The
    stop condition never fired.
  - 11:29:27 the first `Could not publish the order-created event for order <id>: SocketTimeoutException: Connect
    timed out`, logged by the pool threads (`event-publisher-1` and `-2`), and 40 of them up to 11:42:29, one per
    order created while the broker was cut. There were no `Dropped` warnings and no errors, so the queue never
    filled. The first failure came about two minutes in, which fits a connection that was already open when the
    fault began and had to be opened again.
  - No alarm changed state from 11:20 to 11:58: not the four orders-related ones Resilience Hub watched, not
    `region-degraded` in either Region, not any of the ten hop alarms, not `orders-created-zero`.
  - All twelve canaries passed in both Regions through the fault: 279 of 279 runs in us-east-1 and 288 of 288 in
    us-west-2, between 11:25 and 11:48.
  - Resilience Hub spent five more minutes evaluating the success criteria (11:42:48 to 11:47:48) before it ended
    the run `PASSED`, so a passing run takes about 21 minutes from its start.
  - The fault reached the two ECS tasks and blocked the broker's host name; FIS finished the experiment itself
    (`completed`, not halted).
- **2026-10-09, `cart-isolation`, run `2ede1ccb`: `PASSED` after 35 minutes 55 seconds, not as expected (`FAIL`). Only
  the route cut ran; the DynamoDB pause was skipped.** The eight triggers of test2's plan were armed for the run (plan
  version 4, 18:18 to 19:09, 51 minutes; version 5 has none again) and none was met. The live preflight passed,
  including check 8, at 18:20. Times are UTC:
  - 18:21:32 FIS (experiment `EXPL45tih9fu5STXar`) scheduled the template's nine actions. 18:21:50 the route action,
    `aws:network:route-table-disrupt-cross-region-connectivity`, started on three subnets of us-east-1. 18:22:04 the
    other eight were `skipped`, "Target resolution returned empty set": the DynamoDB global table pause, the S3 pause,
    the MemoryDB pause, the Transit Gateway action, the VPC endpoint action, and the SSM, ECS and EKS dependency
    actions. `list-experiment-resolved-targets` returned no table and no bucket, and the report's "Dependencies
    blocked" section said "none listed".
  - 18:29:32 to 18:29:47 the eight `journey-rmt-*` alarms went to `ALARM`, four in each Region: each Region's canaries
    could no longer get through to the other. That is 7 minutes 42 seconds after the route action started.
  - 18:51:53 the route action completed and FIS ended the experiment (`completed`, not halted). us-east-1's four views
    went `OK` at 18:53:47 to 18:54:45, us-west-2's at 18:54:32 (home), 18:58:44 (catalog), 18:59:32 (orders) and 18:59:42
    (cart).
  - No other alarm changed state in either Region: not the four local journeys (the success criteria), not
    `hop-carts-errors` or any other hop alarm, not `dynamodb-errors`, not `region-degraded`. In 285 samples taken about
    every 11 seconds from 18:20:51 to 19:12:43, all six services stayed 2 of 2 (minimum 2) in both Regions, the
    catalog writer stayed in us-east-1 and both ARC health checks stayed healthy.
  - No plan execution started, so `no-plan-execution` passed, and `region-degraded` for us-west-2 stayed `OK`, so
    `destination-stayed-ok` passed. The triggers need a local journey red, and none was: a cut between the two Regions,
    with each serving its own users, starts no failover.
  - Resilience Hub evaluated the success criteria from 18:52:05 to 18:57:05 and ended the run `PASSED`. After the
    10-minute wait for the alarms to settle the report was written at 19:07:48, and the tool exited 3 (the observed
    result is not the expected one).
  - The triggers were switched off again at 19:09, and the checks that come before arming pass in test2 again: plan
    version 5 with no triggers, no execution, all 26 journey and region alarms `OK`, six services 2 of 2 in both
    Regions.
