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

Expected result: FAIL

**Why, while orders publishes on the request thread:** `OrdersEventHandler` publishes the order-created event to
the broker after the order is committed, on the thread that is answering checkout's call. Once the open
connection to the broker is cut, the next publish has to reconnect, and the library's defaults let that block for
up to 60 seconds. Checkout's call to orders reaches the 3-second Service Connect limit first, so checkout returns
an error and ui serves an error page inside the canary's 30-second run. The order itself was saved before the
publish, so `orders-created-zero` stays `OK`.

**What confirms it:** `hop-orders-slow` goes to `ALARM` while `orders-created-zero` stays `OK`, and the orders
journeys fail. In the report's evidence, `hop-checkout-errors` fires as well. The report lays the alarms out by
hop. The order in which they fire does not show where the time went: in the run of 2026-10-07 the ui alarms fired
first, orders' next and checkout's last, all after the fault had already stopped (see Runs).

**What would contradict it:** the journeys stay `OK` (the publish does not block as inferred), or
`orders-created-zero` fires (orders are not being saved, which points at the database rather than the broker).

**Confidence:** confirmed by one run, `bea5d9f4` on 2026-10-07 (see Runs): the journeys failed, the hop alarms of
ui, orders and checkout fired, `orders-created-zero` stayed `OK`, and orders' own log shows the publish blocked on
the request thread. Not seen: the full 15 minutes. The stop condition ended the run 5 minutes 55 seconds after the
fault began, 40 seconds after the first journey alarm, so this run does not say how long the journeys would have
stayed down, or whether Resilience Hub would have ended it `FAILED` without the stop condition.

**After step 9 of the implementation plan** the publish is off the request path and bounded (its own executor, a
2-second connection timeout, a full queue drops the event with a warning), and the expected result becomes
PASS. Step 9 changes the spec and this page in one commit.

## Runs

Four attempts so far, on the first deployment this ran on. The last is the only one that reached a verdict.

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
- **2026-10-07, `orders-broker-dependency`, run `bea5d9f4`: the first verdict, `FAILED` as expected, after 6 minutes
  40 seconds.** Both orders tasks in us-east-1 were registered with SSM (the sidecar image was rebuilt and the
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
