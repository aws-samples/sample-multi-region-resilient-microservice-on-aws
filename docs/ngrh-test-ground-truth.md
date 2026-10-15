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

**First run (2026-10-12, run `5cd025f3`): `PASSED` after 25 minutes 50 seconds, as expected.** The fault was a real one:
FIS ran `aws:ecs:task-network-packet-loss` on catalog's two tasks in us-east-1 from 13:30:52 to 13:51:05, the full 20
minutes (`completed`, nothing stopped it), against the two database endpoints; its EC2 and EKS actions had no targets
and were skipped. The eight triggers of test2's plan were armed from 13:28 to 13:57 (plan versions 8 and 9). Times are
UTC:

- 13:31 the canaries that stay in us-east-1 (`lcl`) and the ones that go through the DNS name (`global`) began to fail for
  catalog and home, and for cart and orders too: all four journeys failed there. The ui load balancer in us-east-1 returned
  a 5xx for every request it received from 13:32 to 13:50 (8 to 12 a minute).
- 13:33:07 to 13:33:58 the eight journey alarms of us-east-1 and its `region-degraded` went to `ALARM`; 13:33:32 to 13:34:42
  the four `journey-rmt-*` alarms of us-west-2 did.
- 13:33:44, **2 minutes 52 seconds after the fault began**, the orders trigger started a graceful deactivate of us-east-1
  (`us-west-2/a2e073cbeafa5ab3`, at us-west-2's endpoint, comment "Execution started by automated trigger [Deactivate
  us-east-1: its orders journey fails there, and us-west-2 sees it fail too while us-west-2 is healthy]"). It started 12
  seconds after its last condition (`journey-rmt-orders` in us-west-2, 13:33:32) was met. The conditions of the catalog,
  home and cart triggers were met at 13:33:52, 13:33:58 and 13:34:42, and ARC started no second execution.
- The plan: the six scale-ups in us-west-2, from 2 to 4 tasks each, ran in parallel and ended after 65 seconds (assets)
  to 191 seconds (carts, 13:36:55); DNS moved from 13:36:55 to 13:38:55; the catalog switchover took 13:38:55 to 13:39:55;
  the application health step took until 13:40:32. The execution ended `completed`, with an ARC recovery time of **6
  minutes 48 seconds** against the objective of 10.
- The global canaries failed in 6 to 7 one-minute runs (13:31 to 13:36 for cart and orders, 13:31 to 13:37 for catalog and
  home), so users of the DNS name saw errors until the DNS answers changed. The `journey-global-*` alarms were `OK` again
  at 13:39:10 (cart), 13:39:16 (orders), 13:40:26 (catalog) and 13:40:32 (home): 8 minutes 18 seconds to 9 minutes 40
  seconds after the fault began, and 6 to 7 minutes after they went red. Resilience Hub's verdict for each: "The alarm
  recovered to OK before the recovery-time deadline and remained OK until the test ended."
- From 13:31 to 14:00 us-west-2 served all of its own canaries, local and global, at 100%, and `region-degraded` for
  us-west-2 never changed. Its `rmt` canaries (its view of us-east-1) failed until the fault ended.
- 13:51:05 the fault ended. The local journeys of us-east-1 were `OK` again from 13:53:52 to 13:54:58 (the canaries
  recovered in the 13:52 minute, the alarms need a few clean minutes) and `region-degraded` at 13:54:58. Resilience Hub
  evaluated the success criteria from 13:51:17 to 13:56:18 and ended the run `PASSED`.
- The tool waited ten minutes, wrote the report at 14:07, saw that the plan had deactivated us-east-1, and failed it back
  once it and its view from us-west-2 had been `OK` for ten minutes: the activate (`us-east-1/971195f1d41e8399`, graceful)
  ran 14:07:31 to 14:09:33, the six us-west-2 services went back from 4 to 2 tasks, and the catalog writer returned to
  us-east-1 in 1 minute 2 seconds. It was done at 14:11:06. The triggers were switched off at 13:57:16, before the
  fail-back, as in the isolation run. By 14:17 test2 was at its baseline (15 of 15 checks).
- The run check `deactivate-completed` passed. The report lists the two database endpoints as dependencies blocked and
  `hop-catalog-slow` as the observability alarm (`ALARM` 13:36:22 to 13:58:22).

**Why:** the fault touches only catalog's tasks in us-east-1. Catalog can no longer read its database, so the
catalog journey and the home journey, which both call catalog through ui, fail from us-east-1 after two of three
one-minute canary runs (on 2026-10-12 cart and orders failed too). That meets one trigger: `journey-lcl-<journey>` for
us-east-1 is red, `journey-rmt-<journey>` for us-west-2 (its view of us-east-1) is red, and `region-degraded` for
us-west-2 is `OK`, because the fault does not reach it. The plan then scales the six services up in us-west-2 to twice their 24-hour peak in us-east-1,
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

**Against the run of 2026-10-12:** all of it held. The run ended `PASSED`; `deactivate-completed` passed; the plan
started 2 minutes 52 seconds after the fault, its nine steps completed, and ARC's recovery time was 6 minutes 48 seconds
against 10; the tool failed us-east-1 back after ten healthy minutes and the report records it.

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
  minimum in the standby Region, would shorten it), or the expectation changes to what the run shows. The first run
  (2026-10-12) was not decided by the clock but came close on its strictest reading: Resilience Hub does not say from
  when it counts its recovery-time deadline; the four global alarms were green 6 to 7 minutes after they went red, and
  8 minutes 18 seconds to 9 minutes 40 seconds after the fault began, the last of them 32 seconds before ten minutes had
  passed since the fault action began running (13:31:04). The plan itself, from its start to green, took 6 minutes 48
  seconds; the 2 minutes 52 seconds from the fault to the plan's start are the detection, and they are what a slower
  alarm or a slower service would lengthen.
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
  runs at was unobserved until 2026-10-12: the first one seen (test2, started by the home trigger during the
  `cart-isolation` run, `us-west-2/caa933cface6ffa3`) was held at the surviving Region's endpoint, us-west-2, like an
  operator-started deactivate, in mode `graceful` and with the same comment shape.
- **A deactivate ends `completed`.** 9 of the demo account's 10 did (one was cancelled), all graceful.

**Not known before the first run, and what the first run (2026-10-12) showed:**

- Whether Resilience Hub only records the plan named in `regionSwitchPlan` (current documentation) or starts it
  itself (the template's own description says "to execute during the test"). **Answered: it records it.** One execution
  started during the run, at 13:33:44 by a trigger, and none from Resilience Hub; the report lists it with its mode and
  comment.
- Whether a trigger-started execution ends `completedMonitoringApplicationHealth` or `completed`; both count. **Answered:**
  `completed`, like a manual deactivate (and like the trigger-started one of the `cart-isolation` run).
- Whether `StartTestRun` accepts the five source alarms. The API says only alarms found during a service assessment
  can be test sources. Catalog's assessment of 2026-09-08 was older than all five alarms (created 2026-10-06), so it
  was assessed again on 2026-10-08 (17:28 to 17:43Z, 19 findings, no cost) before any run. **Answered: it accepts them
  after that assessment.** The run started at 13:30 and Resilience Hub evaluated all four success alarms (all `passed`) and
  the observability alarm. Whether the first assessment's age alone would have refused the alarms was never tested.
- How long from the fault to the start of the plan. **Answered: 2 minutes 52 seconds** (fault 13:30:52, plan 13:33:44),
  of which the first alarm took until 13:33:07 and ARC's own delay from the last condition to the start was 12 seconds.
- Which endpoint an alarm-started deactivate runs at: **us-west-2's**, the surviving Region's, as an operator-started one does.
- Whether the fault leaves the plan able to finish. **Answered: yes.** The fault was at the network level of catalog's
  tasks and the cluster itself stayed available, so the switchover took 60 seconds as without a fault; a fault that also
  slows the primary database is not what this test does, and the paused-switchover case in the README's runbook is still
  unexercised.

**Confidence:** one run, in test2, on 2026-10-12, and it matched the expectation in every point above. `make ngrh-tests`
created the test on 2026-10-08 at 17:02Z, and Resilience Hub accepted its parameters as written (the plan ARN, both
database endpoints, 20 minutes, the impaired and recovery Regions, the log group, no stop condition) and its five alarm
sources. Test2's plan had been updated to this branch's step order, without triggers (2026-10-08 18:23Z, plan version 2
to 3, same ARN): ARC accepted the workflow, the 10-minute objective, the 26 associated alarms and the reports bucket, and
its own evaluation passed at both endpoints with no warnings. The plan and `make failback` had each run once for real
before, by an operator in test2 on 2026-10-08, with no fault and no trigger: a graceful deactivate of us-east-1 completed
in 6 minutes 18 seconds, ARC's report rated it Good with 0 errors, and `make failback REGION=us-east-1` ran from its
preflight to its end without a fix (19:04:25 to 19:08:32Z; activate 2 minutes 6 seconds, writer back in 1 minute 19
seconds). Both switchovers made the Region that gave up the writer fail its catalog-backed journeys for about a minute
(its catalog got `connection refused` and reconnected without restarting a task).

What one run cannot show: the margin. The global alarms were green 9 minutes 40 seconds after the fault began at the
latest, which is inside the objective on every reading but not by much on the strictest one, and a slower scale-up (one
more minute for carts) or a slower alarm would have moved it. The run check cannot tell a plan that is quick from one
that is just fast enough; the report's timeline and ARC's recovery time are what to read, and more runs would say how
much they vary.

## cart-isolation

**Test:** the cart service, multi-Region isolation. For 30 minutes FIS cuts us-east-1 off from us-west-2: it blocks the
cross-Region routes of the subnets it reaches, the Transit Gateway peering and the cross-Region VPC endpoints, and
pauses replication of the S3 buckets, the DynamoDB global table (carts) and any MemoryDB multi-Region cluster it
reaches. The test gives no `dependencies`, so the template's three actions that drop traffic to a DNS name (SSM, ECS
tasks, EKS pods) should have nothing to drop: the documentation says an API caller supplies the list explicitly, but the
template's own parameter text says it is "auto-populated from dependency discovery if enabled" (each service shows 67
discovered dependencies, mostly unrelated traffic), and the first run settled it: the report's "Dependencies blocked"
section was empty, so an omitted list stays empty. What the fault reaches is decided by Resilience Hub's discovery of the cart
service, and the report lists it under "What the fault reached". The service has two input sources: a tag filter
(resources tagged `service` `cart` or `shared`) and, since 2026-10-09, the stack that creates the carts table
(`carts-db-stack${Env}`), which is what makes discovery list the table as a global table (see "How the fault came to
reach the carts table"). The success alarms are the four local journeys of us-east-1 (`journey-lcl-*`): they go through the Region's own load
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
until 2 to 8 minutes after it ended. The run is under Runs. The expectation stayed `FAIL`: the run contradicted
it because the fault did not reach the table, not because the sample behaved differently. The cart service was then given
a second input source, the stack that creates the table (see "How the fault came to reach the carts table"), and the second
run reached the table.

**Second run (2026-10-12, run `8866f947`): `FAILED` after 6.8 minutes, as expected. The DynamoDB pause ran.** FIS resolved
`cartsTable-dev` in us-east-1 as the target of the pause, and the route cut and the pause were the two actions that ran;
the other seven were skipped for lack of targets, as before. The pause was `running` from 11:45:47, the table began to
return system errors at 11:47 (HTTP 500, `InternalServerError`, in carts' log from 11:47:28), and from that minute every
canary in us-east-1 failed, for all four journeys. All eight journey alarms of us-east-1 went to `ALARM` between 11:49:30
and 11:50:57, `region-degraded` for us-east-1 among them at 11:49:32, and that stopped the experiment at 11:49:38. Nothing
changed in us-west-2 except its four views of us-east-1 (`journey-rmt-*`, 11:51:32 to 11:51:44), and
`destination-stayed-ok` passed. The table went on returning errors for two minutes after the stop, and at 11:51:38, two
minutes after the fault had ended, the trigger for the home journey started a graceful deactivate of us-east-1. It finished
at 11:57:35 with a recovery time of 5 minutes 57 seconds against the objective of 10, the tool failed us-east-1 back
between 12:12 and 12:16, and test2 was at its baseline at 12:22. The run is under Runs.

**Why:** the sample does not keep serving in us-east-1 when its carts table loses the other Regions (inferred from the
sources below, and borne out by the second run), because of four facts.

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

The alarms were predicted to change in this order: `dynamodb-errors` first (one 60-second period), then `hop-carts-errors`
and the journeys (two failed minutes out of three), then `region-degraded`, which stops the experiment. On 2026-10-12 the
journeys and `region-degraded` changed as predicted, but `dynamodb-errors` did not change at all and `hop-carts-errors`
changed three minutes after the stop (11:52:29). The table did return the errors; the alarm could not see them (see "A defect
found in the DynamoDB alarms"). FIS documents that replication may go on for up to five minutes after the action begins;
here the pause was `running` 2 minutes 5 seconds after it was scheduled and the first errors were counted in the minute
that began 1 to 2 minutes later. Nothing changed in us-west-2 apart from its views of us-east-1: the pause isolates
us-east-1's replica, and us-west-2's replica and the witness still form the quorum, so us-west-2 kept serving and its
`region-degraded` stayed `OK`. (The blog's example has three replicas; a witness in the place of the third replica gave
the same result.)

**What the plan does about it:** the live preflight requires the triggers (check 8), so a journey that fails locally can
start a failover. Each of the four triggers that deactivate us-east-1 needs its journey red in us-east-1, red in
us-west-2's view of us-east-1, and `region-degraded` for us-west-2 `OK`. The first holds, the second holds because of
the cut itself, and the third by the paragraph above, so a trigger should fire and the plan should fail us-east-1 over to
us-west-2 (graceful, with the catalog switchover last). `make ngrh-test` then waits for us-east-1 to be healthy for ten
minutes and runs `make failback`. The stop condition and the triggers raced on 2026-10-12, and the stop condition won: it
ended the fault at 11:49:38, but DynamoDB went on returning errors for two minutes ("briefly" in its documentation), which
kept us-east-1's journeys red until us-west-2's views of them turned red at 11:51:32. The trigger fired at 11:51:38, two
minutes after the fault had ended and two minutes before the journeys were `OK` again (11:53:30 to 11:53:58). So the plan
failed us-east-1 over for a fault that was already over, which is what triggers on alarms do: they act on the alarms'
state, and that lags the fault by the alarms' evaluation periods and by whatever the fault leaves behind.

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

**Against the run of 2026-10-12:** all but the second of these held. The run ended `FAILED` after 6.8 minutes, the FIS
experiment was `stopped` (11:49:38) and `region-degraded-us-east-1` was among the alarms that changed (11:49:32). The carts
table was among what the fault reached. `destination-stayed-ok` passed. `no-plan-execution` failed by listing one deactivate
of us-east-1, started by a trigger (the home journey's), whose steps completed, with a recovery time of 5 minutes 57
seconds. The tool failed us-east-1 back (12:12:20 to 12:15:52). The second did not hold: `dynamodb-errors-us-east-1` did
not change, because the alarm could not see the errors (fixed since, and deployed to test2 on 2026-10-12, see below).

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

**Not known before the first run, and what the runs showed:**

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
  that this is the reason was not checked). **Answered again 2026-10-12, with the table's stack as a second input
  source: the three subnets of us-east-1 and the carts table.** Discovery listed 25 resources, one more than before
  (an `AWS::DynamoDB::GlobalTable`, `cartsTable-dev`), and the pause's resolved target was the table's ARN in us-east-1.
  The S3 pause again resolved no bucket, and the other six actions again had no target.
- Whether an omitted `dependencies` list stays empty. **Answered 2026-10-09: it stays empty.** The report's
  "Dependencies blocked" section says "none listed", and the SSM, ECS and EKS actions were skipped for lack of targets.
- How long after the action begins the errors start (FIS says up to five minutes) and how long after the stop they go
  on. **Answered for the route cut only:** the cross-Region views failed 7 minutes 42 seconds after the action began
  (route action running 18:21:50, first alarm 18:29:32) and recovered 2 to 8 minutes after it completed (18:51:53):
  us-east-1's four views at 18:53:47 to 18:54:45, us-west-2's at 18:54:32 (home), 18:58:44 (catalog), 18:59:32 (orders)
  and 18:59:42 (cart). **For the DynamoDB pause (2026-10-12):** scheduled 11:43:10, `initiating` 11:43:42, `running`
  11:45:47, first errors in the 11:47 minute, first alarm 11:49:30; FIS stopped it at 11:49:37 and errors were still
  counted at 11:50 (6 on each of the two operations) and 11:51 (3). The journey alarms of us-east-1 were `OK` again from
  11:53:30 to 11:53:58, about four minutes after the stop.
- What ARC does with four triggers that are met in the same minutes: start one execution, start four, or start one and
  refuse the rest. The report lists every execution that started, and `make ngrh-test` fails each deactivated Region
  back once. **Answered 2026-10-12: it started one.** The conditions of the home and orders triggers held from 11:51:32,
  those of cart and catalog from 11:51:42 and 11:51:44. ARC started one execution, at 11:51:38, with the home trigger's
  description in its comment, and no other execution is listed at either endpoint.
- Which endpoint a trigger-started deactivate runs at, and what its comment says (only activates had been seen).
  **Answered 2026-10-12:** the surviving Region's endpoint (us-west-2), like an operator-started deactivate, in mode
  `graceful`, with the comment "Execution started by automated trigger [<the trigger's description>]".
- Whether Resilience Hub accepts the five source alarms when a run starts. The API takes only alarms found in a
  failure-mode assessment of the service as test sources, and the alarms were created on 2026-10-06. The cart service
  was assessed again on 2026-10-09 (13:35 to 13:50Z), and CloudTrail shows the discovery session asking CloudWatch about
  all five during it (`hop-carts-errors` and the four `journey-lcl-*` alarms of us-east-1). Orders' first run was
  refused until its service had been assessed after its alarms existed, and once more for one alarm tagged outside its
  scope, which the preflight now checks. **Answered 2026-10-09: the assessment was enough.** The run started, Resilience
  Hub evaluated the four success alarms (all `passed`) and the observability alarm, and ended the run `PASSED`.
- Control-plane calls on the table, such as `UpdateTable`, are blocked in every Region while the pause lasts (the
  blog), and FIS allows at most 5,040 minutes of this action per MRSC table in a rolling seven days. Do not deploy the
  table during a run; the limit allows over a hundred 30-minute runs a week. The second run paused the table for 3
  minutes 50 seconds (11:45:47 to 11:49:37); whether `UpdateTable` was refused in that time was not tried. Afterwards
  the table was `ACTIVE` with `STRONG` consistency in both Regions, both replicas and the witness in us-east-2 `ACTIVE`
  (read at 11:59).
- Whether carts fails with the error the blog describes. **Answered 2026-10-12:** carts' log in us-east-1 has
  `InternalServerErrorException: Internal server error (Service: AmazonDynamoDBv2; Status Code: 500; Error Code:
  InternalServerError)` from 11:47:28, thrown out of the request, and the CloudWatch metric `SystemErrors` (and
  `FaultInjectionServiceInducedErrors`) for `cartsTable-dev` counts the same errors on `Query` and `GetItem`.
  `dynamodb:InjectError` conditioned on `dynamodb:FisActionId` was accepted: the action ran and injected errors.

**How the fault came to reach the carts table (answered 2026-10-12).** The table is an `AWS::DynamoDB::GlobalTable`
(logical id `DDBGlobalTable`, physical name `cartsTable-dev`, in the stack `carts-db-stack-dev`). The cart service's
input source was a tag filter, and discovery by tag lists the table's replica in each Region as an
`AWS::DynamoDB::Table`. The service now has a second input source, the stack itself (`cfnStackArn`, next to
`resourceTags` in the same union), and discovery lists an `AWS::DynamoDB::GlobalTable` as well, which the pause takes as
its target: the second run showed both. In test2 the source was added through the API on 2026-10-09 (19:37Z); discovery
listed the table about a minute later and the assessment of 19:54Z kept it. `ngrh.yaml` gets the same source through its
`CartsDbStackArn` parameter, which `make ngrh` fills in from the stack when the stack exists (and warns when it does not,
in which case the isolation test is the route cut alone). That template change has not been deployed to test2. The live
service has the API-added source and an older design-file source that the template does not declare, and what the
CloudFormation handler does with a service that differs from its template is unknown, so no change set for `ngrh-dev`
has been created. A fresh deployment has no such difference, and the first one made from this template will be the
first test of the parameter in CI.

**What the test cannot show:** whether the plan restores service while the fault goes on. The stop condition ends the
fault about two minutes after users first see errors (11:47 to 11:49:38) and a failover takes about six minutes, so the
fault was over before the failover moved a request: the journeys of us-east-1 were `OK` again at 11:53:58, before the
plan's DNS step began (11:54:39). The failover ran for a fault that had already ended. `catalog-recovery`, which has no
stop condition and a 20-minute fault, is the test for that.

**A defect found in the DynamoDB alarms, and its fix (deployed to test2 on 2026-10-12).** `dynamodb-errors-${Region}${Env}`
is a composite of `dynamodb-user-errors` and `dynamodb-system-errors`, and it did not change state on 2026-10-12 although
the table returned system errors for four minutes. `dynamodb-system-errors` alarmed on `SystemErrors` with the single
dimension `TableName`, but CloudWatch holds that metric for `cartsTable-dev` only with the dimensions `TableName` and
`Operation` (`Query` and `GetItem` here: 33 and 33 errors in the 11:47 minute, then 44 and 44, 34 and 35, 6 and 6, and 3),
so the alarm received no data and treated the missing data as not breaching ("no datapoints were received for 1 period").
`dynamodb-user-errors` had the same kind of problem: `UserErrors` is published once per account and Region with no
dimension at all (`list-metrics` returns one series, without dimensions), so a `TableName` dimension selected nothing.
Both alarms are in the repository's first commit (`45e6b04`) and in `main`. The report lists `dynamodb-errors` among the
evidence, so it said "no state change" for it whatever the table did.

The fix keeps the names, the threshold (any error in one 60-second period), the missing-data setting, the tag and the
composite, and changes what the two alarms look at:

- `dynamodb-system-errors` is a metric math alarm, `SUM(METRICS())` over `SystemErrors` with `TableName` and `Operation`
  for each operation the carts service sends: `GetItem` (mapper `load`), `Query` (`query`), `UpdateItem` (`save`),
  `DeleteItem` (`delete`) and `BatchWriteItem` (`batchDelete`), and `PutItem` for another save behavior. The table's
  `SuccessfulRequestLatency` series in test2 are exactly the first five. The sum skips a series with no data in the
  period, so an operation that has never failed costs nothing.
- `dynamodb-user-errors` has no dimension, so it now watches every table in the account and Region. DynamoDB counts
  HTTP 400 errors in it except failed conditional writes, which have their own metric.

Replaying the new expression with `get-metric-data` (the same queries the alarm holds, read-only) on the 11:40 to 12:00
window of 2026-10-12 gives 66, 88, 69, 12 and 3 for the minutes 11:47 to 11:51, and no data for the quiet windows after
it. The sum is above zero from the minute 11:47, which began two and a half minutes before the first journey alarm
changed (11:49:30). The alarm was not deployed yet, so this was a check of the expression, not of the alarm. The
alarms now see more than they did, and not only in fault tests: in the 14 days before the run, `SystemErrors` for this
table had data in three hours (`GetItem` 5 errors on 09-28, `Query` 1 on 09-28 and `GetItem` 7 on 10-01), and
`UserErrors` in one (2 errors on 09-29), so with the threshold unchanged they would have fired a few times in two weeks
on single errors. Whether any error should page is a choice that the fix leaves where it was.

`tests/test_dynamodb_alarms.py` pins the shape, and a scan of the carts Java source keeps the alarm's operations equal to
what the code calls (a new call fails the test until the alarm covers it); 82 deliberate breaks of the template, the
Java and the scan are caught (`temporary/mutations/dynamodb_alarms.py`, outside the repository). To use it, `make
monitoring` has to deploy `monitoring.yml` in each Region. In test2 that was done from change sets created on 2026-10-12
at 14:55Z (one per Region, two rows each, neither alarm replaced) and executed at 22:39Z in us-west-2 and at 22:40Z in
us-east-1. Both stacks ended `UPDATE_COMPLETE`, and only the two alarms changed, in place. On 2026-10-15 the deployed
`dynamodb-system-errors` alarm's own query returned, over the previous 14 days, 66, 88, 69, 12 and 3 for the minutes
11:47 to 11:51 of 2026-10-12 in us-east-1 (the numbers of the replay above) and three single-error minutes in us-west-2.

**What it would mean:** a `FAILED` run as expected says that us-east-1 cannot serve on its own while its carts table
cannot reach the quorum, which the second run showed. Whether the failover would keep users served while the fault goes
on this test cannot say (see "What the test cannot show"). Reads could continue if carts fell back to an
eventually consistent read when a strongly consistent one fails (the blog suggests this); writes could not, which is
the price of strong consistency, so the cart and orders journeys would still fail locally.

**Confidence:** two runs exist. The first (2026-10-09) did not reach the table; the second (2026-10-12) did and ended
`FAILED`, as expected, so the result rests on one run that reached the table. The explanation was inferred from AWS's
documentation and the repository's code, with the table's mode and the Region's health read from test2, and the run
bore it out: the table returned HTTP 500 to carts, which logged the exception, and all four journeys failed in the
Region. `make ngrh-tests` created the test
in test2 on 2026-10-09 at 13:35Z, and Resilience Hub accepted its parameters as written (both Regions, 30 minutes, the
log group, the stop condition) and its five alarm sources. It echoed no `dependencies` list, which said that none was
stored, and the run confirmed that none is used. A second reconcile found nothing to change, the static preflight
passed, and the live preflight refused the test only at check 8, because test2's plan had no triggers (with them armed
for the run, it passed). The experiment
role in test2 already has every permission the FIS documentation lists for this action, including
`dynamodb:InjectError` for multi-Region strong consistency tables. That statement is conditioned on
`dynamodb:FisActionId`, a key the DynamoDB service reference does define for `InjectError`; that the request carries it
with this action's name was assumed by analogy with the EC2 example in the FIS documentation, and the second run
tested it: the action ran and the table returned errors. That ui calls carts on every page is what makes all four
journeys fail rather than only the two that add to a cart; the canaries showed all four failing.

## Runs

Eight attempts so far, on the first deployment this ran on: five of `orders-broker-dependency`, two of
`cart-isolation` and one of `catalog-recovery`. The last two of orders reached a verdict: the first `FAILED`, as then expected, and the last
`PASSED`, as expected now. The first run of `cart-isolation` `PASSED` where `FAIL` is expected, because the fault did not
reach the carts table; the second reached it and `FAILED`, as expected. The one run of `catalog-recovery` `PASSED`, as expected.

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
- **2026-10-12, `cart-isolation`, run `8866f947`: `FAILED` after 6.8 minutes (Resilience Hub's start to its end), as
  expected (`FAIL`). The DynamoDB pause ran.** The cart service had the carts table's stack as a second input source
  since 2026-10-09. The eight triggers of test2's plan were armed from 11:40:37 and switched off again at 12:03:41, before
  the fail-back (which needs none, and whose switchover back to us-east-1 gives us-west-2 a blip that could otherwise
  have started a second failover). The live preflight passed, including check 8, at 11:41. Times are UTC:
  - 11:43:09 FIS (experiment `EXPs1PnXtJVD2q3x6v`) scheduled the nine actions. The route action ran from 11:43:29. At
    11:43:42 seven of the others were `skipped`, "Target resolution returned empty set", and the DynamoDB pause was
    `initiating`; it was `running` from 11:45:47. `list-experiment-resolved-targets` returned the table `cartsTable-dev`
    in us-east-1 for `dynamodb-global-tables`, the three subnets for `subnets`, and no bucket.
  - 11:47 the table began to return system errors in us-east-1 (33 on `Query` and 33 on `GetItem` in that minute, then 44
    and 44, 34 and 35, 6 and 6, 3), carts logged `InternalServerErrorException` from 11:47:28, and every canary of
    us-east-1 failed from that minute, for all four journeys, the global ones and the local ones. The ui load balancer
    returned 5xx for 35 of 39 requests from 11:47 to 11:50.
  - 11:49:30 to 11:50:57 the eight journey alarms of us-east-1 went to `ALARM` (global orders first, local cart last),
    and `region-degraded` for us-east-1 at 11:49:32. 11:49:36 and 11:49:37 FIS stopped the two actions and 11:49:38 the
    experiment ("Experiment halted by stop condition."). Resilience Hub ended the run at 11:49:50 with "failure".
  - 11:51:32 to 11:51:44 the four `journey-rmt-*` alarms of us-west-2 went to `ALARM`: its views of us-east-1 still saw
    errors, which the table went on returning for two minutes after the stop. 11:51:38 the trigger for the home journey
    started a graceful deactivate of us-east-1 (`us-west-2/caa933cface6ffa3`). It finished at 11:57:35: scale-up in
    us-west-2 11:51:38 to 11:54:39 (carts the slowest, ui the fastest at 11:52:43), DNS 11:54:39 to 11:56:39, catalog
    switchover 11:56:39 to 11:57:34. Recovery time 5 minutes 57 seconds against the objective of 10. ARC's PDF report is
    in the reports bucket, the first one written by the plan's report configuration in a real execution.
  - After the stop the hop alarms fired: `hop-ui-errors` 11:51:39, `hop-ui-slow` 11:51:58, `hop-carts-errors` 11:52:29,
    `hop-carts-slow` 11:52:37. All were `OK` again by 11:58:29 except `hop-ui-errors`, which cleared at 12:08:39. The
    journey alarms of us-east-1 and `region-degraded` were `OK` again from 11:53:30 to 11:53:58, four minutes after the
    stop and before the plan's DNS step began. `dynamodb-errors` never changed (see "A defect found in the DynamoDB
    alarms").
  - 11:57 to 11:58 the switchover gave us-east-1 about a minute of failed requests (ui 5xx 8 and 3 in two minutes, the
    local canaries at 0% in the 11:57 minute) and turned the us-west-2 views of us-east-1 red again from 11:59:32 to
    12:01:42. No local journey alarm fired. Nothing else changed state in either Region.
  - The tool waited until us-east-1 and its view from us-west-2 had been `OK` for ten minutes (from 12:01:42), then ran
    the fail-back: the activate took 12:12:20 to 12:14:23, the six us-west-2 services went back from 4 to 2 tasks, and
    the catalog writer moved back in 1 minute 2 seconds. It was done at 12:15:52. The switchover back gave us-east-1 a
    minute of failed requests at 12:15 (ui 5xx 7), and us-west-2's catalog canaries failed in the 12:16 minute.
  - The run checks: `no-plan-execution` failed (the execution above, which is the plan doing its job) and
    `destination-stayed-ok` passed. The carts table was `ACTIVE` with `STRONG` consistency in both Regions at 11:59.
    By 12:22 test2 was at its baseline: plan version 7 without triggers, no execution going (four finished ones), six
    services 2 of 2 in both Regions, the writer in us-east-1, both ARC health checks healthy, 26 journey and
    region-degraded alarms `OK`. The one exception was `hop-ui-errors` in us-west-2, which went to `ALARM` at 12:19:02,
    four minutes after the 5xx of the fail-back's switchover (one at 12:15).
- **2026-10-12, `catalog-recovery`, run `5cd025f3`: `PASSED` after 25 minutes 50 seconds, as expected (`PASS`).** The first
  alarm-started failover with a real fault. Test2's plan had its eight triggers armed from 13:28:08 to 13:57:16 (plan
  versions 8 and 9); the static and live preflight passed at 13:28 and 13:30, check 8 included. The full account is under
  "First run" in the `catalog-recovery` section: fault 13:30:52 to 13:51:05 on catalog's two tasks in us-east-1, the
  orders trigger started a graceful deactivate of us-east-1 at 13:33:44 (2 minutes 52 seconds after the fault began), it
  completed at 13:40:32 with an ARC recovery time of 6 minutes 48 seconds against 10, the four global alarms were green
  again from 13:39:10 to 13:40:32, and the tool failed us-east-1 back between 14:07 and 14:11 (activate about 2 minutes,
  writer back in 1 minute 2 seconds) after the triggers had been switched off. Test2 was at its baseline at
  14:17 (15 of 15 checks). The first trigger-started failover of 2026-10-12 11:51 (the `cart-isolation` run) had taken
  5 minutes 57 seconds from its start.
