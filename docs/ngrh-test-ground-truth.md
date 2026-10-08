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
the test is for. The report lists each check with its reason.

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
  budget is tight (the design estimates 6.5 to 11.5 minutes: 3 to 4 to detect, a minute to scale up, up to a minute
  for the DNS change, up to a minute and a half for the next canary run, 3 to 4 for the alarms to clear), and ARC
  adds two delays it does not publish, from the alarms to the start of the plan and from the plan to the health
  checks. The report's timeline shows where the time went. Either the alarms or the plan get faster, or the
  expectation changes to what the run shows.
- The run check fails with no deactivate: no trigger fired. The evidence alarms say which of the three conditions
  was missing. If the plan has no triggers the deployment was made with `AUTOMATIC_FAILOVER=disabled`, and
  preflight refuses the run before it starts.
- The deactivate paused: the switchover could not finish, most likely because the fault also slowed the primary
  database, and the execution waits in `pausedByFailedStep` with traffic already moved. The README's runbook has the
  three ways out, and `make failback` refuses until the execution is resolved.

**Not known before the first run:**

- Whether Resilience Hub only records the plan named in `regionSwitchPlan` (current documentation) or starts it
  itself (the template's own description says "to execute during the test"). The report lists every execution that
  started during the run with its mode and comment, and a second execution started a moment after the first would
  be this.
- What `executionRegion` means in `list-plan-executions` and `get-plan-execution`. The API documents it only as "the
  Region for a plan execution". The run check, the choice of which Region `run` fails back and the live preflight
  all read it as the Region the execution targets, so a deactivate of us-east-1 has `executionRegion` us-east-1,
  which is how `StartPlanExecution`'s `targetRegion` reads. If it is the Region whose endpoint ran the execution, the
  check fails a correct run, and the fix is one line in `executions.py`. The timed, operator-started failover that
  comes before this test settles it.
- Whether `completedMonitoringApplicationHealth` or `completed` is the state a trigger-started execution ends in;
  both count.
- What comment, if any, ARC records on an execution its triggers started. Nothing reads it; the report prints it.
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
in us-east-1 in 24 hours was 4, so the scale-up asks for at most 8 of 10). No run exists, and the plan's triggers
and the order of its steps have only been tested against a fake of the AWS calls. The first live run is the check.

## Runs

Five attempts so far, on the first deployment this ran on. The last two reached a verdict: the first `FAILED`, as
then expected, and the last `PASSED`, as expected now.

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
