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
