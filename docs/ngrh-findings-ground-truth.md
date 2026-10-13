# NGRH Findings Ground Truth

> Baseline assessment results for the AWS Resilience Hub (NGRH v2) model
> configured on this repository. Use this document to evaluate whether future
> assessment runs, agent-driven remediation, or architecture changes improve,
> regress, or maintain the resilience posture.

## Metadata

| Field | Value |
|-------|-------|
| Assessment date | 2026-10-13 |
| Deployment | The sample alone in a clean sandbox account (tag discovery sees every tagged resource of an account, so no other deployment of the sample may share it), `ENV=-ngrh`, us-east-1 (primary) and us-west-2 (standby), built from this change before it merged |
| Stack | ngrh-ngrh |
| Services | 6 (ui, catalog, cart, checkout, orders, assets) |
| Policies | Tier-1: RTO 10 min, RPO 0 · Tier-2: RTO 10 min, RPO 5 min |
| Tier1 services | ui, cart, orders |
| Tier2 services | catalog, checkout, assets |
| Journeys | 4: Browse Catalog, Manage Cart, Checkout & Place Order, View Orders (ui and cart are in all four) |
| Discovery | By tag: a service's resources carry `service` = its own name or `shared`; cart also reads the stack that creates the carts table. ui 127 · catalog 148 · cart 134 · checkout 129 · orders 137 · assets 125 resources |
| Failover triggers | Armed (`AUTOMATIC_FAILOVER=enabled`): eight alarm triggers on the plan |
| Assessments | One failure mode assessment per service, 01:56 to 02:13 UTC, four hours after the stack was created |
| Total findings | 59 (HIGH 9 · MEDIUM 36 · LOW 14) |
| Assertions active | 43: 16 written in `deployment/ngrh.yaml` (ui 2 · catalog 4 · cart 2 · checkout 2 · orders 3 · assets 3) and 27 that Resilience Hub generated |

How this baseline was produced: `make build-images` and `make deploy AUTOMATIC_FAILOVER=disabled` (as the e2e workflow does),
then `make region-switch-plan AUTOMATIC_FAILOVER=enabled` once the journey alarm replay showed that no trigger would have fired,
four hours of discovery, and `make ngrh-assess ENV=-ngrh`. The findings below are the OPEN findings of each service's latest
assessment (`list-failure-mode-findings`, detail from `get-failure-mode-finding`), in the order the API returned them. Two edits were
made to NGRH's text because this file is public: the sandbox account number is written `123456789012`, and the two Route 53 health
check ids are written `<health-check-id>`. Everything else, resource names included, is as NGRH wrote it.

## Architecture Context

- **Deployment**: Active/Active, us-east-1 (primary) + us-west-2 (standby)
- **Compute**: ECS Fargate on demand (`FARGATE`), two tasks per service in each Region (auto scaling 2 to 10). There is no Spot capacity, so an interruption cannot cut a fault test short
- **Discovery**: by tag (see Metadata). The ECS cluster has no `service` tag, because all six services run on it and tagging it would make each service discover the others through it
- **ARC Region Switch**: the plan scales up the remaining Region, moves DNS, then switches the catalog database over (no data loss), and `make failback` brings a Region back. While `AUTOMATIC_FAILOVER` is `enabled` (the default) eight alarm triggers start it: a journey that fails in one Region, confirmed from the other while that one is healthy, deactivates the failing Region, not more than once in 60 minutes; `disabled` leaves out the triggers and the permission to start the plan, so an operator starts it
- **Pre-scaling**: 200% of containerInsightsMaxInLast24Hours before failover proceeds (15 minutes allowed)
- **Canaries and alarms**: twelve CloudWatch Synthetics canaries per Region (four pages: home, cart, catalog and orders, each requested from its own Region, from the other Region and through the global name) feed the journey alarms that the triggers use; hop alarms show which hop failed
- **Data**: Aurora Global DB (catalog), DynamoDB Global Table with multi-Region strong consistency and a witness Region (cart), Aurora DSQL (orders), ElastiCache Redis (checkout, ephemeral), RabbitMQ (orders, SINGLE_INSTANCE)
- **Resilience tests**: `orders-broker-dependency`, `catalog-recovery` and `cart-isolation`, defined in `deployment/ngrh-tests.json`, with their expected results in `docs/ngrh-test-ground-truth.md`. They replaced the hand-built FIS experiment (the `chaos-engineering` make target)

## Findings at a glance

| Service | Tier | Journeys | Findings | HIGH | MEDIUM | LOW |
|---------|------|----------|----------|------|--------|-----|
| ui | 1 | 4 | 10 | 2 | 5 | 3 |
| catalog | 2 | 1 | 11 | 0 | 8 | 3 |
| cart | 1 | 4 | 11 | 3 | 7 | 1 |
| checkout | 2 | 1 | 10 | 1 | 7 | 2 |
| orders | 1 | 2 | 10 | 2 | 6 | 2 |
| assets | 2 | 1 | 7 | 1 | 3 | 3 |
| Total | | | 59 | 9 | 36 | 14 |

The HIGH findings:

| Service | # | Title |
|---------|---|-------|
| ui | 2 | ARC Region Switch plan sequential step ordering delays user-facing recovery beyond RTO |
| ui | 7 | ALB target groups lack fail-open configuration risking total regional traffic loss |
| cart | 6 | DynamoDB Global Tables async replication cannot meet declared RPO of 0 minutes |
| cart | 7 | ECS scaling step 15-minute timeout exceeds 10-minute RTO budget |
| cart | 10 | DynamoDB Global Table deletion protection disabled risks accidental table loss |
| checkout | 9 | ElastiCache Redis single-node per region with no failover or cross-region replication |
| orders | 1 | Insufficient ECS task count risks major capacity loss from single AZ impairment |
| orders | 7 | Gray AZ failures may cause prolonged degradation without triggering regional failover |
| assets | 4 | ARC Region Switch sequential step ordering exceeds 10-minute RTO |

The findings fall into recurring themes. The table groups them by title (the patterns are in
`tests/test_ngrh_findings_ground_truth.py`); the 2026-06-17 column counts the previous baseline the same way.

| Theme | 2026-10-13 | 2026-06-17 | Services (2026-10-13) |
|-------|------------|------------|-----------------------|
| ECS runs on Fargate Spot only | 0 | 6 | none |
| Deletion protection off (ALBs, Aurora, DSQL, DynamoDB table) | 7 | 6 | ui, catalog, cart (2), checkout, orders, assets |
| ALB client keep-alive left at the 3600 second default | 6 | 4 | ui, catalog, cart, checkout, orders, assets |
| ALB target groups without fail-open | 4 | 3 | ui, catalog, checkout, assets |
| Route 53 accelerated recovery off | 6 | 1 | ui, catalog, cart, checkout, orders, assets |
| ECS health check grace period of zero | 6 | 0 | ui, catalog, cart, checkout, orders, assets |
| Two tasks per Region or two of three AZs used | 5 | 3 | ui, catalog, cart, checkout, orders |
| One ALB shared by several services | 3 | 0 | cart, orders, assets |
| CloudWatch agent sidecar marked essential | 2 | 0 | ui, checkout |
| Failover time or step timeouts against the 10-minute RTO | 4 | 2 | ui, catalog, cart, assets |
| No AWS Backup plan or backup snapshots | 4 | 4 | catalog, cart, checkout, orders |
| ElastiCache Redis without replicas or failover | 1 | 1 | checkout |
| RabbitMQ without high availability or cross-Region replication | 1 | 1 | orders |
| S3 buckets without versioning | 2 | 0 | checkout, orders |
| Gray AZ failures not handled | 2 | 0 | cart, orders |
| Other, one-off findings | 6 | 4 | ui (2), catalog (3), cart |

The one-off findings are: ui 5 "ARC Region Switch report bucket is single-region risking failover workflow disruption"; ui 10 "High target group healthy threshold slows capacity recovery during scaling"; catalog 2 "ECS auto-scaling may be insufficient for single-region operation during failover"; catalog 3 "Aurora Serverless v2 minimum capacity of 1 ACU risks cold-start latency under sudden load"; catalog 5 "Aurora DB instances have identical PromotionTier preventing deterministic failover"; cart 6 "DynamoDB Global Tables async replication cannot meet declared RPO of 0 minutes".

## Findings

## ui (10 findings)

### 1. ALB deletion protection disabled risks accidental infrastructure loss
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have deletion_protection.enabled set to false. Accidental deletion of either ALB via console, CLI, or IaC misconfiguration would immediately sever all traffic to the UI service in that region, causing a complete regional outage until the ALB is recreated and DNS propagates.
- **Mitigations:**
  - Enable deletion_protection on ALB apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and ALB apps-ngr-Alb-qjQDE37pXLaM in us-west-2.
- **Observability:**
  - Use AWS Config rules to continuously monitor that deletion_protection.enabled remains true on both ALBs. Alert via SNS if the setting is changed.
- **Testing:**
  - Attempt to delete one of the ALBs via CLI after enabling deletion protection to confirm the safeguard prevents the operation.

### 2. ARC Region Switch plan sequential step ordering delays user-facing recovery beyond RTO
- **Severity:** HIGH
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The ARC Region Switch deactivate workflow (mr-rs-plan-ngrh) executes three sequential steps: (1) ECS scaling with 15-minute timeout, (2) Route 53 DNS shift with 5-minute timeout, (3) Aurora Global Database switchover with 10-minute timeout. DNS traffic shift — the action that recovers user-facing traffic — cannot begin until ECS scaling completes. Worst-case sum of timeouts is 30 minutes.
- **Mitigations:**
  - Reorder the deactivate workflow in ARC Region Switch plan mr-rs-plan-ngrh so that the Route53HealthCheck execution block (shift-dns-traffic-away) is the first step, before the ECS scaling step. This shifts traffic away from the impaired region immediately, achieving user-facing recovery in seconds rather than waiting for scaling to complete.
  - Move the ECS scaling execution block (scale-up-ecs-services) to run as the second step after DNS shift. Since both regions are already serving traffic with 2 tasks each, the surviving region can absorb initial traffic while scaling to 200% proceeds in the background.
- **Observability:**
  - Monitor the ARC Region Switch plan execution duration via the execution reports written to S3 bucket region-switch-ngrh-reportsbucket-kgglku40vdri. Create a CloudWatch alarm that triggers if total plan execution time exceeds 8 minutes, providing early warning before the 10-minute RTO is breached.
- **Testing:**
  - Execute the ARC Region Switch plan mr-rs-plan-ngrh in a controlled failover drill targeting each region. Measure the wall-clock time from plan trigger to DNS shift completion and compare against the 10-minute RTO. Validate that the surviving region serves traffic correctly before ECS scaling completes.

### 3. Route 53 hosted zone has accelerated recovery disabled slowing DNS failover
- **Severity:** LOW
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The private hosted zone for store.demo-ngrh.io has EnableAcceleratedRecovery set to false. Accelerated recovery reduces the time Route 53 takes to detect and respond to health check failures, enabling faster DNS failover between regions.
- **Mitigations:**
  - Enable accelerated recovery on hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io) to reduce DNS failover detection and propagation time for the store.demo-ngrh.io latency-based routing records.
- **Observability:**
  - Monitor Route 53 health check status change events via CloudWatch. Track the time delta between health check failure and DNS record update to measure actual failover latency.
- **Testing:**
  - Execute the ARC Region Switch plan deactivate workflow for one region and measure the end-to-end time from trigger to DNS resolution change. Compare with and without accelerated recovery enabled.

### 4. ECS tasks cover only 2 of 3 Availability Zones in each region
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Each region runs only 2 ECS tasks across 3 configured AZs, so targets are present in only 2 AZs. In us-east-1, targets exist in us-east-1b and us-east-1d only. In us-west-2, targets exist in us-west-2a and us-west-2c only. If the AZ hosting one of the two targets is impaired, 50% of regional capacity is lost and the single remaining target must absorb all traffic.
- **Mitigations:**
  - Increase the minimum task count for the ECS service ui-ngrh in us-east-1 (arn:aws:ecs:us-east-1:123456789012:service/apps-ngrh-EcsCluster-5KPggqxgwMcr/ui-ngrh) to at least 3 so that tasks are distributed across all 3 configured AZs (us-east-1a, us-east-1b, us-east-1d). Update the auto-scaling minimum capacity from 2 to 3.
  - Increase the minimum task count for the ECS service ui-ngrh in us-west-2 (arn:aws:ecs:us-west-2:123456789012:service/apps-ngrh-EcsCluster-kSVyjSlYruf7/ui-ngrh) to at least 3 so that tasks are distributed across all 3 configured AZs (us-west-2a, us-west-2b, us-west-2c). Update the auto-scaling minimum capacity from 2 to 3.
- **Observability:**
  - No additional observability recommendations for this finding. The existing ECS AZ rebalancing and ALB ATW configuration provide operational visibility into target distribution.
- **Testing:**
  - After increasing the minimum task count to 3, verify that ECS places one task in each of the 3 AZs by inspecting the target group registrations on apps-n-AlbTa-78XXUDCQAIGN in us-east-1 and apps-n-AlbTa-F8JMDIHZAMNF in us-west-2. Confirm all 3 AZs show healthy targets.

### 5. ARC Region Switch report bucket is single-region risking failover workflow disruption
- **Severity:** LOW
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The ARC Region Switch plan writes execution reports to S3 bucket region-switch-ngrh-reportsbucket-kgglku40vdri located only in us-east-1. If us-east-1 is the impaired region, the plan's report-writing step may fail or be delayed, potentially affecting plan execution completion status.
- **Mitigations:**
  - Create a matching reports S3 bucket in us-west-2 and update the ARC Region Switch plan mr-rs-plan-ngrh ReportConfiguration to use a bucket in each region, or enable S3 Cross-Region Replication on the existing bucket to ensure report availability regardless of which region is impaired.
- **Observability:**
  - Monitor S3 error metrics (4xxErrors, 5xxErrors) on bucket region-switch-ngrh-reportsbucket-kgglku40vdri. Alert if write errors occur during an ARC Region Switch plan execution.
- **Testing:**
  - During a failover drill that deactivates us-east-1, verify that the ARC Region Switch plan completes successfully even if the reports bucket in us-east-1 is unreachable. Confirm whether report-writing failure blocks plan completion.

### 6. Zero ECS health check grace period causes premature task termination during startup
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ECS services have HealthCheckGracePeriodSeconds set to 0. The UI task definition includes an init container dependency and a Java application with OTEL instrumentation agent requiring significant startup time. With a target group HealthyThresholdCount of 5 and HealthCheckIntervalSeconds of 30, registration alone takes 150 seconds. A zero grace period means ECS may terminate tasks still starting up.
- **Mitigations:**
  - Set HealthCheckGracePeriodSeconds to at least 180 seconds on ECS service ui-ngrh in us-east-1 (cluster apps-ngrh-EcsCluster-5KPggqxgwMcr). This accounts for the init container, Java application startup with OTEL agent, and the 150-second target group registration period.
  - Apply the same HealthCheckGracePeriodSeconds of 180 seconds on ECS service ui-ngrh in us-west-2 (cluster apps-ngrh-EcsCluster-kSVyjSlYruf7).
- **Observability:**
  - Monitor ECS service events for task replacement patterns. Create a CloudWatch alarm on the ECS service RunningTaskCount metric, alerting when it drops below the desired count for more than 2 consecutive evaluation periods, which may indicate a task cycling issue.
- **Testing:**
  - Force a rolling restart of the ECS service by triggering a new deployment and observe task startup timing. Measure the time from task launch to the first healthy ALB health check to validate the grace period is sufficient.

### 7. ALB target groups lack fail-open configuration risking total regional traffic loss
- **Severity:** HIGH
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both target groups (us-east-1 and us-west-2) have unhealthy_state_routing.minimum_healthy_targets.count set to 1 and percentage set to off. With cross-zone load balancing enabled and only 2 targets per region, if health checks fail on both targets simultaneously (e.g., transient dependency issue causing deep health check failure), the ALB stops routing traffic entirely.
- **Mitigations:**
  - Set target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to 50 on target group apps-n-AlbTa-78XXUDCQAIGN in us-east-1. This ensures the ALB fails open and continues routing traffic when more than half the targets are unhealthy, preventing complete outage from coordinated health check failures.
  - Apply the same minimum_healthy_targets.percentage of 50 on target group apps-n-AlbTa-F8JMDIHZAMNF in us-west-2.
- **Observability:**
  - Create CloudWatch alarms on the HealthyHostCount metric for both target groups, alerting when the count drops below 2. This provides early warning of health check failures before the fail-open threshold is reached.
- **Testing:**
  - Use AWS FIS with the aws:ecs:task-network-blackhole action to simulate network connectivity loss to downstream microservices from the UI tasks. Verify that the ALB fails open and continues routing traffic rather than marking all targets unhealthy.

### 8. Default ALB keep-alive of 3600 seconds delays client recovery during impairments
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs in us-east-1 and us-west-2 have client_keep_alive.seconds set to the default value of 3600 seconds (1 hour). Clients maintain persistent connections to ALB nodes for up to an hour, meaning if an ALB node or its AZ becomes impaired, clients continue sending requests to the impaired node for up to 60 minutes before re-establishing connections.
- **Mitigations:**
  - Reduce client_keep_alive.seconds to 180 on ALB apps-ngr-Alb-sVmLp0H1caWm in us-east-1. This forces clients to drop and re-establish connections every 3 minutes, enabling faster traffic redistribution during impairments.
  - Apply the same client_keep_alive.seconds value of 180 on ALB apps-ngr-Alb-qjQDE37pXLaM in us-west-2.
- **Observability:**
  - Monitor the ALB ActiveConnectionCount and NewConnectionCount metrics for both ALBs. A sudden drop in new connections or sustained high active connections during an impairment indicates clients are stuck on long-lived connections.
- **Testing:**
  - During a planned failover test using the ARC Region Switch plan, measure how quickly client traffic redistributes after DNS shift. Compare connection drain times with the current 3600-second setting versus the recommended 180-second setting.

### 9. CloudWatch agent sidecar marked essential creates shared fate with UI container
- **Severity:** MEDIUM
- **Category:** SHARED_FATE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** In both regions, the ecs-cwagent container is marked as Essential: true in the task definition. If the CloudWatch agent sidecar crashes or becomes unhealthy, ECS will terminate the entire task including the healthy UI container. With only 2 tasks per region, this creates shared fate between observability infrastructure and the primary request-serving container.
- **Mitigations:**
  - Change the ecs-cwagent container to Essential: false in task definition apps-ngrh-UiTaskDefinition-yqsKnSSXTH3d in us-east-1. This allows the UI container to continue serving traffic even if the CloudWatch agent crashes.
  - Apply the same change to the ecs-cwagent container in task definition apps-ngrh-UiTaskDefinition-0pTWwhCz2pMX in us-west-2.
- **Observability:**
  - Create a CloudWatch alarm on the ECS task-level container health status for the ecs-cwagent container. Alert when the sidecar stops so operators can investigate without impacting the UI service.
- **Testing:**
  - Use AWS FIS with the aws:ecs:task-kill action targeting the ecs-cwagent container to verify that the UI container continues serving requests when the sidecar fails. Confirm ALB health checks remain healthy.

### 10. High target group healthy threshold slows capacity recovery during scaling
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both target groups have HealthyThresholdCount set to 5 with HealthCheckIntervalSeconds of 30 seconds. A new or replacement task requires 150 seconds (2.5 minutes) of consecutive healthy checks before receiving traffic, on top of container startup time. With only 2 running tasks per region, losing one task leaves 50% capacity for an extended period.
- **Mitigations:**
  - Reduce HealthyThresholdCount from 5 to 2 on target group apps-n-AlbTa-78XXUDCQAIGN in us-east-1. This reduces registration time from 150 seconds to 60 seconds while still requiring two consecutive healthy checks for confidence.
  - Apply the same HealthyThresholdCount of 2 on target group apps-n-AlbTa-F8JMDIHZAMNF in us-west-2.
- **Observability:**
  - Monitor the ALB TargetResponseTime metric and the target group HealthyHostCount. Alert when HealthyHostCount drops below the desired task count for more than 3 minutes, indicating slow target registration is impacting capacity.
- **Testing:**
  - Trigger an ECS service scale-out event and measure the end-to-end time from task launch to first request served. Compare with the reduced HealthyThresholdCount to validate the improvement in capacity recovery time.

## catalog (11 findings)

### 1. Route 53 Accelerated Recovery not enabled on hosted zone
- **Severity:** LOW
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The Route 53 hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io) has EnableAcceleratedRecovery set to false. If the Route 53 control plane in us-east-1 becomes unavailable, DNS record changes cannot be made from any region. Accelerated Recovery would allow regaining control plane access via us-west-2 within 60 minutes.
- **Mitigations:**
  - Enable Accelerated Recovery on hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io). This provides the ability to regain Route 53 control plane access via us-west-2 within 60 minutes if us-east-1 is impaired, serving as a safety net for post-failover DNS management.
- **Observability:**
  - Monitor the Route 53 health checks (<health-check-id> and <health-check-id>) associated with the latency-based routing records. Alert immediately when either health check transitions to unhealthy.
- **Testing:**
  - After enabling Accelerated Recovery, verify it is functional by checking the hosted zone features in the Route 53 console. Test the failover path by triggering the ARC Region Switch plan and confirming DNS shifts correctly via the data-plane health checks without requiring control plane operations.

### 2. ECS auto-scaling may be insufficient for single-region operation during failover
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** Both ECS services have autoScalingMaxCapacity of 10 with a baseline DesiredCount of 2. The ARC Region Switch plan pre-scales to 200% (4 tasks), but if the surviving region must absorb all production traffic, the gap between the pre-scaled 4 tasks and the max of 10 relies on auto-scaling reacting quickly enough. If the service was already scaling up to handle peak load before failover, the 200% pre-scale target (4 tasks) could actually be a reduction from the current running count.
- **Mitigations:**
  - Review the ARC Region Switch plan's pre-scaling target to ensure it accounts for peak traffic scenarios, not just 200% of baseline DesiredCount. Consider setting the pre-scale target to 200% of the auto-scaling max capacity observed during peak periods, or increase autoScalingMaxCapacity on both ECS services to provide additional headroom during failover.
  - Evaluate using Fargate capacity providers with a mix of FARGATE and FARGATE_SPOT to ensure baseline capacity is always available, or consider Fargate capacity reservations if available for the task size.
- **Observability:**
  - Create CloudWatch alarms on ECS CPUUtilization and the auto-scaling TargetTrackingScaling metrics for both services. Alert when RunningTaskCount approaches autoScalingMaxCapacity (e.g., 8 out of 10), indicating the service is nearing its scaling ceiling.
  - Monitor ALB TargetResponseTime and HTTPCode_ELB_5XX_Count on both ALBs to detect capacity-related degradation during failover.
- **Testing:**
  - Execute the ARC Region Switch plan during a simulated peak traffic period using load testing. Verify that the surviving region scales to handle the full combined traffic within the 10-minute RTO and that task count does not hit the autoScalingMaxCapacity ceiling.

### 3. Aurora Serverless v2 minimum capacity of 1 ACU risks cold-start latency under sudden load
- **Severity:** LOW
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** Both Aurora Serverless v2 clusters are configured with MinCapacity of 1 ACU and MaxCapacity of 8 ACU. During low-traffic periods, instances may scale down to 1 ACU. A sudden traffic surge or regional failover (doubling load on the surviving region) requires rapid scaling from 1 ACU, which introduces latency as Aurora provisions additional capacity incrementally in 0.5 ACU steps.
- **Mitigations:**
  - Increase the MinCapacity on both Aurora Serverless v2 clusters to 2 ACU. This ensures a baseline capacity that can handle moderate traffic without cold-start scaling delays, particularly important during regional failover scenarios where the surviving cluster must absorb doubled load.
- **Observability:**
  - Monitor the Aurora ServerlessDatabaseCapacity CloudWatch metric for both clusters. Create alarms when ACU utilization exceeds 75% of MaxCapacity (6 ACU), indicating the cluster is approaching its scaling ceiling. Also monitor DatabaseConnections and DMLLatency to detect performance degradation during scaling events.
- **Testing:**
  - Run a load test that simulates a sudden doubling of read traffic against one Aurora cluster (mimicking regional failover). Measure query latency during the scaling period from MinCapacity to the required capacity level, and verify it stays within acceptable bounds for the catalog API response time SLO.

### 4. ECS health check grace period set to zero risks premature task termination
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ECS services have HealthCheckGracePeriodSeconds set to 0. The catalog container has a health check with a 60-second StartPeriod, 10-second interval, and 3 retries. With a zero grace period, the ALB target group health check may mark the task unhealthy before the container finishes starting, causing ECS to terminate and replace it in a restart loop.
- **Mitigations:**
  - Set HealthCheckGracePeriodSeconds to at least 90 seconds on both ECS services catalog-ngrh in us-east-1 and us-west-2. This value should cover the container StartPeriod (60s) plus the ALB target group healthy threshold check time, giving the application sufficient time to initialize before ECS acts on health check failures.
- **Observability:**
  - Monitor the ECS service event log and CloudWatch metrics for repeated task start/stop cycles. Create an alarm on the ECS service RunningTaskCount metric that triggers when it drops below the DesiredCount for more than 2 consecutive evaluation periods.
- **Testing:**
  - Force a new deployment of the ECS service and observe task registration timing. Verify that new tasks pass both the container health check and ALB target group health check before the grace period expires, and that no tasks are prematurely terminated during rollout.

### 5. Aurora DB instances have identical PromotionTier preventing deterministic failover
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** All four Aurora DB instances across both clusters have PromotionTier set to 1. Within each cluster, when the writer instance fails, Aurora selects the failover target non-deterministically among instances with the same promotion tier. While both instances are db.serverless with equivalent capacity, having the same tier removes operator control over which instance becomes the writer.
- **Mitigations:**
  - Set PromotionTier to 0 on one instance in each cluster (e.g., catalog-dbcluster-01-ngrh-2 in us-east-1 and catalog-dbcluster-02-ngrh-2 in us-west-2) to designate them as the preferred failover targets. Keep the other instances at PromotionTier 1.
- **Observability:**
  - Monitor the Aurora RDS event notifications for failover events on both clusters. Track which instance was promoted to writer to validate that the promotion tier configuration produces the expected failover behavior.
- **Testing:**
  - Use the AWS FIS action aws:rds:failover-db-cluster to trigger a failover on each Aurora cluster. Verify that the instance with PromotionTier 0 is consistently promoted to writer and that the catalog service recovers within acceptable latency bounds.

### 6. Aurora Global Database promotion adds fixed time cost against 10-minute RTO
- **Severity:** MEDIUM
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** Aurora Global Database requires a secondary cluster promotion (control plane operation, typically 1-2 minutes) when failing over from us-east-1. During a Regional impairment, the us-west-2 secondary cluster must be promoted to writer before it can serve write-forwarded traffic or act as a standalone writer. This promotion is a fixed time cost that consumes 10-20% of the 10-minute RTO budget and depends on the RDS control plane in us-west-2 being available.
- **Mitigations:**
  - Since the catalog service is read-only in-region per the assertions, consider configuring the us-west-2 ECS tasks to connect to the secondary cluster's reader endpoint directly for read traffic, bypassing the need for immediate promotion for read-only workloads. Promotion would only be needed if writes (via global write forwarding) are required post-failover.
  - Ensure the ARC Region Switch plan (mr-rs-plan-ngrh) includes the Aurora Global Database failover execution block to automate promotion without manual intervention, minimizing the fixed time cost.
- **Observability:**
  - Monitor the CloudWatch metric AuroraGlobalDBReplicationLag on cluster catalog-dbcluster-02-us-west-2-ngrh. Alert when replication lag exceeds 30 seconds, as high lag during promotion increases data divergence risk.
  - Track ARC Region Switch plan execution duration via CloudWatch metrics to confirm total failover time stays within the 10-minute RTO budget.
- **Testing:**
  - Conduct a quarterly failover drill by executing the ARC Region Switch plan (mr-rs-plan-ngrh) to measure end-to-end failover time including Aurora promotion, ECS scaling, and DNS convergence. Validate total time remains under 10 minutes. Use AWS FIS action aws:rds:failover-db-cluster to simulate Aurora cluster failures.

### 7. ECS tasks span only 2 of 3 Availability Zones in each region
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ECS services have DesiredCount=2 with 3 configured subnets. With only 2 tasks, capacity exists in at most 2 of 3 AZs at any time. If the AZ hosting one of the two tasks is impaired, 50% of regional compute capacity is immediately lost. While AZ rebalancing is enabled, replacing the impaired task depends on ECS control plane availability and Fargate capacity, which may be delayed during an AZ impairment.
- **Mitigations:**
  - Increase the minimum task count for the ECS service catalog-ngrh in us-east-1 (arn:aws:ecs:us-east-1:123456789012:service/apps-ngrh-EcsCluster-5KPggqxgwMcr/catalog-ngrh) to at least 3 so that tasks are distributed across all 3 configured AZs (us-east-1a, us-east-1b, us-east-1d). Update both DesiredCount and autoScalingMinCapacity accordingly.
  - Increase the minimum task count for the ECS service catalog-ngrh in us-west-2 (arn:aws:ecs:us-west-2:123456789012:service/apps-ngrh-EcsCluster-kSVyjSlYruf7/catalog-ngrh) to at least 3 so that tasks are distributed across all 3 configured AZs (us-west-2a, us-west-2b, us-west-2c). Update both DesiredCount and autoScalingMinCapacity accordingly.
- **Observability:**
  - Monitor the ECS service metric RunningTaskCount for both catalog-ngrh services, alarming when it drops below the desired count. Use the AvailabilityZone dimension on ALB target group metrics (HealthyHostCount, UnHealthyHostCount) to detect per-AZ target health degradation on the target groups associated with apps-ngr-Alb-sVmLp0H1caWm and apps-ngr-Alb-qjQDE37pXLaM.
- **Testing:**
  - Use AWS FIS with the aws:ecs:task-stop action to terminate a single ECS task in one AZ and verify that the remaining task handles full regional load while ECS replaces the stopped task. Measure time to replacement and confirm no request failures during the recovery window.

### 8. No AWS Backup plan protecting Aurora Global Database against correlated data corruption
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The Aurora Global Database clusters have native 7-day automated backups but no AWS Backup plan for centralized, policy-driven backup management. Without AWS Backup, there is no protection against ransomware or accidental bulk data corruption that could replicate from us-east-1 to us-west-2 via the global replication layer, compromising both clusters simultaneously. Affected resources: catalog-dbcluster-01-us-east-1-ngrh, catalog-dbcluster-02-us-west-2-ngrh, catalog-global-db-cluster-ngrh.
- **Mitigations:**
  - Create an AWS Backup plan that includes both Aurora clusters: catalog-dbcluster-01-us-east-1-ngrh in us-east-1 and catalog-dbcluster-02-us-west-2-ngrh in us-west-2. Enable continuous backups with point-in-time recovery for Aurora within the AWS Backup plan to support the 5-minute RPO target.
  - Configure an AWS Backup vault with vault lock in compliance mode to create immutable backup copies that cannot be deleted even by privileged users, protecting against ransomware and insider threats.
  - Add a cross-account or cross-region copy rule in the AWS Backup plan to store backup copies in an isolated account or additional region, ensuring recoverability even if the source account is compromised.
- **Observability:**
  - Monitor the AWS Backup CloudWatch metrics aws/backup NumberOfBackupJobsFailed and NumberOfCopyJobsFailed for the Aurora backup plan. Set alarms to trigger on any value greater than 0 to detect backup failures promptly.
  - Create a CloudWatch alarm on the aws/backup BackupJobDuration metric to alert if backup jobs exceed expected duration thresholds, indicating potential issues with the Aurora clusters.
- **Testing:**
  - Perform a quarterly restore test by restoring the Aurora cluster from an AWS Backup recovery point to a new cluster in a non-production environment. Validate data integrity by comparing row counts and checksums against the source cluster, and measure the restore duration against the 10-minute RTO target.

### 9. ALB client keep-alive at default 3600s delays failover from impaired load balancers
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs (us-east-1 and us-west-2) have client_keep_alive.seconds set to the default 3600 seconds. Clients maintain persistent connections to a potentially impaired ALB for up to one hour before re-establishing connections, delaying recovery when the ALB or its AZ is degraded.
- **Mitigations:**
  - Set client_keep_alive.seconds to 180 on ALB apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and ALB apps-ngr-Alb-qjQDE37pXLaM in us-west-2. This forces clients to re-establish connections every 3 minutes, enabling faster redistribution away from impaired ALB nodes while keeping connection reuse overhead manageable.
- **Observability:**
  - Monitor the ALB ActiveConnectionCount and NewConnectionCount metrics for both ALBs. Alert if ActiveConnectionCount remains elevated while HealthyHostCount drops, which indicates clients are holding connections to an impaired path.
- **Testing:**
  - Simulate an impaired ALB node by blocking traffic to one AZ's ALB subnet using a network ACL, then measure how quickly client connections redistribute. Compare recovery time with the default 3600s versus the recommended 180s setting.

### 10. Deletion protection disabled on ALBs, Aurora clusters, and Global Database
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Deletion protection is disabled on both ALBs (deletion_protection.enabled=false), both Aurora DB clusters (DeletionProtection=false), and the Aurora Global Database cluster (DeletionProtection=false). Accidental deletion of any of these critical resources via API calls, CLI commands, or infrastructure-as-code misconfigurations would cause immediate service outage in the affected region or globally.
- **Mitigations:**
  - Enable deletion protection on both ALBs: apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and apps-ngr-Alb-qjQDE37pXLaM in us-west-2.
  - Enable deletion protection on both Aurora DB clusters: catalog-dbcluster-01-us-east-1-ngrh and catalog-dbcluster-02-us-west-2-ngrh, and on the Global Database cluster catalog-global-db-cluster-ngrh.
- **Observability:**
  - Use AWS Config rules to continuously evaluate that deletion protection remains enabled on ALBs and RDS clusters. Alert via SNS when any resource is found non-compliant.
- **Testing:**
  - Attempt to delete each protected resource via the AWS CLI or console and verify that the deletion is blocked with an appropriate error message confirming deletion protection is active.

### 11. ALB target group missing fail-open configuration for coordinated health check failures
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have cross-zone load balancing enabled and their target groups use default minimum_healthy_targets settings (count=1, percentage=off). With only 2 ECS tasks per region, a coordinated health check failure (e.g., transient database connectivity issue) could mark all targets unhealthy, causing the ALB to return 503 errors to all requests instead of failing open.
- **Mitigations:**
  - Configure target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to 50 on the target groups for both ALBs. With cross-zone enabled and 2 targets, this ensures the ALB fails open and continues routing traffic when health checks experience coordinated failure, rather than returning 503 to all clients.
- **Observability:**
  - Create CloudWatch alarms on the UnHealthyHostCount metric for both target groups. Alert when UnHealthyHostCount equals the total target count (2), indicating all targets are unhealthy simultaneously — the exact scenario minimum_healthy_targets protects against.
- **Testing:**
  - Use AWS FIS with the aws:ecs:task-network-blackhole action to block database connectivity from all ECS tasks simultaneously. Verify that with the fail-open configuration, the ALB continues routing requests rather than returning 503 errors, and that requests succeed once database connectivity is restored.

## cart (11 findings)

### 1. ALB deletion protection disabled risks accidental infrastructure loss
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both regional ALBs have deletion_protection.enabled set to false. Accidental deletion of either ALB via console, CLI, or IaC pipeline would immediately sever the traffic path for that region's cart service, causing complete regional unavailability until the ALB is recreated and DNS propagates.
- **Mitigations:**
  - Enable deletion protection on ALB apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and ALB apps-ngr-Alb-qjQDE37pXLaM in us-west-2 by setting the deletion_protection.enabled attribute to true.
- **Observability:**
  - Configure AWS CloudTrail alerts for DeleteLoadBalancer API calls targeting these ALBs. Alert immediately on any attempt to delete these resources.
- **Testing:**
  - After enabling deletion protection, verify it works by attempting to delete one of the ALBs via CLI in a non-production environment and confirming the operation is rejected.

### 2. No AWS Backup plan protecting DynamoDB Global Table against ransomware or correlated corruption
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The DynamoDB Global Table cartsTable-ngrh has PITR enabled in both regions but no AWS Backup plan. PITR alone does not protect against ransomware or bulk accidental deletion scenarios where corrupted data replicates sub-second across both Global Table replicas via active/active replication, making both PITR streams contain the corruption. AWS Backup with vault lock provides isolated, immutable backup copies.
- **Mitigations:**
  - Create an AWS Backup plan that includes the DynamoDB Global Table cartsTable-ngrh in both us-east-1 and us-west-2. Configure snapshot-based backups at a frequency appropriate for the 0-minute RPO goal (e.g., hourly snapshots).
  - Enable AWS Backup Vault Lock on the backup vault storing cartsTable-ngrh backups to provide immutable, tamper-proof backup copies that protect against ransomware and accidental or malicious deletion of backups.
  - Configure cross-account backup copy rules to store backup copies in a separate AWS account, providing additional isolation against account-level compromise.
- **Observability:**
  - Create CloudWatch alarms on the AWS Backup metrics NumberOfBackupJobsFailed and NumberOfBackupJobsCompleted for the cartsTable-ngrh resource to detect backup failures promptly. Alert when any backup job fails or when expected backup jobs do not complete within the scheduled window.
- **Testing:**
  - Perform a quarterly restore test of cartsTable-ngrh from AWS Backup to a new table name. Validate that the restored data is consistent and that the restore completes within the 10-minute RTO. Measure the actual restore duration and data integrity against the source table to confirm recoverability.

### 3. Shared ALB across multiple microservices creates DNS-layer shared fate
- **Severity:** MEDIUM
- **Category:** SHARED_FATE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Each regional ALB is shared across six microservices (carts, orders, assets, catalog, checkout, ui) via path-based or host-based routing. If any one target group becomes unhealthy in an AZ, the ALB may remove its IP from DNS for that AZ, affecting all six microservices behind the same load balancer including the carts service.
- **Mitigations:**
  - Configure the target_group_health.dns_failover.minimum_healthy_targets.count and target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage on each target group behind these ALBs. Setting unhealthy_state_routing minimum_healthy_targets.percentage to 50 for cross-zone enabled target groups prevents congestive collapse while limiting the blast radius of individual target group health issues.
  - Consider separating microservices with significantly different fleet sizes or criticality levels onto dedicated ALBs. For example, place the carts service on its own ALB to isolate it from failures in other microservices' target groups.
- **Observability:**
  - Monitor the HealthyHostCount and UnHealthyHostCount CloudWatch metrics for each target group behind both ALBs. Create alarms when HealthyHostCount drops below expected thresholds per target group to detect early signs of shared-fate DNS removal.
- **Testing:**
  - Simulate a target group health failure for a non-carts microservice behind the shared ALB using AWS FIS to inject network disruption to those targets. Verify that the carts target group continues to receive traffic and is not affected by DNS-level changes triggered by the other target group's unhealthy state.

### 4. ECS carts service runs only two tasks across three AZs causing uneven capacity distribution
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both regional ECS carts services run 2 tasks across 3 AZs with a minimum auto-scaling capacity of 2. Two tasks cannot be evenly distributed across three AZs — one AZ always has zero tasks while two AZs each have one task. If the AZ hosting one of the two tasks becomes impaired, 50% of the regional compute capacity is immediately lost.
- **Mitigations:**
  - Increase the minimum desired count for the carts-ngrh ECS service from 2 to 3 in both regions (us-east-1 and us-west-2). Update both the DesiredCount and autoScalingMinCapacity to 3. With AZ rebalancing enabled, ECS will place one task per AZ, ensuring that losing any single AZ removes at most 33% of capacity.
- **Observability:**
  - Monitor the RunningTaskCount metric for the carts-ngrh ECS service in both regions using CloudWatch Container Insights. Create an alarm that triggers when RunningTaskCount drops below 3 (the recommended minimum) for more than 1 minute, indicating potential AZ-level capacity loss.
- **Testing:**
  - After increasing the minimum task count to 3, verify that ECS places exactly one task per AZ in each region. Then use AWS FIS with the aws:ecs:stop-task action to stop the task in a single AZ and confirm that the remaining two tasks absorb traffic without errors or significant latency increase on the ALB.

### 5. ALB default keep-alive delays client recovery from impaired load balancer nodes
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both regional ALBs (apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and apps-ngr-Alb-qjQDE37pXLaM in us-west-2) have client_keep_alive.seconds set to the default 3600 seconds. Clients maintain persistent connections to potentially impaired load balancer nodes for up to one hour, delaying recovery when an ALB node or its AZ becomes degraded.
- **Mitigations:**
  - Set the client_keep_alive.seconds attribute to 180 on ALB apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and ALB apps-ngr-Alb-qjQDE37pXLaM in us-west-2. This forces clients to re-establish connections more frequently, enabling faster redistribution away from impaired ALB nodes.
- **Observability:**
  - Monitor the ActiveConnectionCount and NewConnectionCount CloudWatch metrics on both ALBs. Create alarms when ActiveConnectionCount drops sharply or NewConnectionCount spikes abnormally, which may indicate connection cycling issues or ALB node impairment.
- **Testing:**
  - Conduct a load test simulating persistent client connections and inject a partial ALB node failure using AWS FIS action aws:network:disrupt-connectivity. Measure how quickly clients redistribute to healthy nodes with the reduced keep-alive setting versus the default 3600 seconds.

### 6. DynamoDB Global Tables async replication cannot meet declared RPO of 0 minutes
- **Severity:** HIGH
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The cartsTable-ngrh DynamoDB Global Table uses standard asynchronous replication (eventual consistency). The resilience policy requires RPO = 0 minutes. Asynchronous replication inherently means RPO > 0 — writes accepted in one region may not yet be replicated to the other region at the moment of a regional impairment, resulting in data loss.
- **Mitigations:**
  - Enable Multi-Region Strong Consistency (MRSC) on the cartsTable-ngrh DynamoDB Global Table. MRSC provides synchronous cross-Region replication with RPO = 0 by requiring write acknowledgment from both Regions before confirming to the client. Note this will increase write latency due to cross-Region round-trips.
- **Observability:**
  - Monitor the DynamoDB ReplicationLatency metric for cartsTable-ngrh in both us-east-1 and us-west-2. Set a CloudWatch alarm when replication latency exceeds a threshold (e.g., 1 second sustained for 3 consecutive evaluation periods) to detect replication degradation before a failover event.
- **Testing:**
  - Perform a failover drill by executing the ARC Region Switch plan to deactivate one Region, then immediately verify cart data consistency by comparing items written just before failover in the deactivated Region against the surviving Region's replica. Measure actual data loss to quantify the RPO gap under realistic conditions.

### 7. ECS scaling step 15-minute timeout exceeds 10-minute RTO budget
- **Severity:** HIGH
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The ARC Region Switch plan mr-rs-plan-ngrh executes ECS scaling to 200% as the first step (scale-up-ecs-services) with a 15-minute timeout, before the DNS traffic shift step. The DNS shift only begins after scaling completes. If scaling takes close to its timeout, the total failover time will exceed the declared 10-minute RTO.
- **Mitigations:**
  - Reorder the ARC Region Switch plan deactivate workflow so the Route53HealthCheck DNS shift step executes before or in parallel with the ECS scaling step. This shifts traffic to the surviving Region immediately while scaling proceeds, reducing time-to-recovery. The surviving Region already has 2 running tasks that can absorb initial traffic while scaling to 200% completes.
  - Reduce the ECS scaling execution block TimeoutMinutes to a value that fits within the RTO budget (e.g., 8 minutes), leaving headroom for the DNS shift step.
- **Observability:**
  - Monitor ARC Region Switch plan execution duration and step-level timing from the execution reports stored in the region-switch-ngrh-reportsbucket-kgglku40vdri S3 bucket. Create a CloudWatch alarm on total plan execution time exceeding 8 minutes to detect RTO breaches.
- **Testing:**
  - Execute the ARC Region Switch plan in a controlled failover drill and measure end-to-end execution time, specifically the duration of the scale-up-ecs-services step. Verify that total recovery time from trigger to traffic serving in the surviving Region stays within the 10-minute RTO. Run the drill during peak traffic hours to test scaling under realistic load.

### 8. Route 53 accelerated recovery disabled slows DNS failover propagation
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The Route 53 hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io) has EnableAcceleratedRecovery set to false. During a regional failover triggered by the ARC Region Switch plan, DNS record changes to shift traffic away from the impaired region will propagate at standard Route 53 speeds rather than the accelerated rate.
- **Mitigations:**
  - Enable accelerated recovery on Route 53 hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io) by setting EnableAcceleratedRecovery to true. This reduces DNS propagation time during failover events, helping meet the 10-minute RTO target.
- **Observability:**
  - Monitor Route 53 health check status changes for health checks <health-check-id> (us-east-1) and <health-check-id> (us-west-2). Alert when either health check transitions to unhealthy to track failover initiation timing.
- **Testing:**
  - Execute a failover drill using the ARC Region Switch plan to deactivate one region. Measure the end-to-end time from alarm trigger to all client traffic arriving at the surviving region. Compare DNS propagation time with and without accelerated recovery enabled.

### 9. ECS health check grace period of zero may cause premature task termination during startup
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ECS services (carts-ngrh in us-east-1 and us-west-2) have HealthCheckGracePeriodSeconds set to 0. The carts container health check has a StartPeriod of 240 seconds, indicating the Java Spring Boot application takes significant time to initialize. With zero grace period, the ALB target group health check may mark new tasks as unhealthy before the application is ready, causing ECS to terminate and replace them in a restart loop.
- **Mitigations:**
  - Set HealthCheckGracePeriodSeconds to at least 300 seconds on both ECS services carts-ngrh in us-east-1 and us-west-2. This value should exceed the container's StartPeriod of 240 seconds plus the ALB target group health check registration time to ensure tasks are not terminated before they can pass health checks.
- **Observability:**
  - Monitor ECS service events for repeated task start/stop cycles on carts-ngrh in both regions. Create CloudWatch alarms on the ECS service RunningTaskCount metric when it drops below the desired count for more than 5 minutes, indicating potential health check churn.
- **Testing:**
  - Force a rolling replacement of ECS tasks on carts-ngrh by updating the task definition and observe whether new tasks successfully pass health checks and begin serving traffic without being prematurely terminated. Measure the time from task launch to first successful health check.

### 10. DynamoDB Global Table deletion protection disabled risks accidental table loss
- **Severity:** HIGH
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The DynamoDB Global Table cartsTable-ngrh has DeletionProtectionEnabled set to false in both us-east-1 and us-west-2 replicas. An accidental or unauthorized DeleteTable API call could permanently destroy the table and all its data in both regions, as Global Table replica deletion cascades. Even with PITR enabled, restoring a deleted Global Table requires recreating the table, restoring from PITR, and re-establishing global replication — a process that would far exceed the 10-minute RTO.
- **Mitigations:**
  - Enable DeletionProtectionEnabled on the DynamoDB Global Table cartsTable-ngrh. This must be set on both the us-east-1 and us-west-2 replicas to prevent accidental deletion of either replica or the global table.
- **Observability:**
  - Configure an AWS CloudTrail-based CloudWatch alarm or EventBridge rule to detect UpdateTable API calls that attempt to disable deletion protection on cartsTable-ngrh. Alert the operations team immediately when such changes are detected.
- **Testing:**
  - After enabling deletion protection, verify it works by attempting a DeleteTable API call against cartsTable-ngrh in a controlled manner and confirming the call is rejected with a ValidationException. Incorporate this check into infrastructure compliance tests.

### 11. Gray AZ failures may not trigger regional failover leaving no within-region recovery
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The architecture relies exclusively on regional failover via ARC Region Switch to recover from AZ impairments. Gray AZ failures (elevated latency, intermittent errors affecting one AZ) may degrade service without triggering the journey-level alarm conditions required for regional failover. During such events, approximately one-third of within-region capacity is impaired with no mechanism to shift traffic away from the affected AZ.
- **Mitigations:**
  - Enable ALB Automatic Target Weights (ATW) with anomaly mitigation on the carts target groups for both ALBs. ATW can automatically detect and reduce traffic to targets experiencing elevated errors within seconds, complementing the regional failover strategy without requiring over-provisioning. This requires the ALB load balancing algorithm to be set to weighted_random and cross-zone load balancing to remain enabled.
  - As an alternative or complement to ATW, consider adding per-AZ health monitoring that publishes CloudWatch metrics for AZ-level error rates and latency on each ALB. Create CloudWatch alarms on these per-AZ metrics that can feed into operational runbooks for manual intervention when gray failures are detected but regional failover is not warranted.
- **Observability:**
  - Create CloudWatch alarms on per-AZ metrics for both ALBs, specifically monitoring HTTPCode_Target_5XX_Count and TargetResponseTime broken down by the AvailabilityZone dimension. Alert when a single AZ shows error rates or latency significantly above the other AZs (e.g., 3x the p99 latency of peer AZs sustained for 2 minutes).
- **Testing:**
  - Use AWS FIS with the aws:ecs:task-network-blackhole-port action targeting carts tasks in a single AZ to simulate a gray AZ failure. Verify whether the regional failover alarms trigger and measure the duration of customer-visible impact. This validates whether the current regional-failover-only strategy adequately covers partial AZ impairments for the carts-ngrh service.
  - Use the AWS FIS aws:az:power-interruption scenario targeting a single AZ in us-east-1 to simulate a full AZ impairment. Measure time-to-detection and time-to-recovery via the ARC Region Switch plan, and verify the total impact duration stays within the 99.99% SLO budget.

## checkout (10 findings)

### 1. ECS tasks limited to 2 per region across 3 AZs risks capacity loss during AZ impairment
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Each region runs only 2 ECS Fargate tasks across 3 AZs. With 2 tasks and 3 AZs, at least one AZ has no task. If the AZ hosting one of the two tasks becomes impaired, 50% of the region's checkout capacity is lost, leaving a single task to handle all regional traffic until auto-scaling adds capacity.
- **Mitigations:**
  - Increase the minimum desired count for the checkout-ngrh ECS service to at least 3 in both regions (us-east-1 and us-west-2). With 3 tasks and 3 AZs, the AZ rebalancing feature can place one task per AZ, so losing one AZ removes only 33% of capacity instead of 50%.
- **Observability:**
  - Monitor the ECS service RunningTaskCount metric for checkout-ngrh in both regions. Create a CloudWatch alarm that triggers when RunningTaskCount drops below the desired count for more than 2 minutes, which could indicate task failures in an impaired AZ.
- **Testing:**
  - Use AWS FIS with the aws:ecs:task-kill action targeting checkout tasks in a single AZ to simulate AZ-scoped task loss. Verify that the remaining tasks handle traffic without errors and that ECS auto-scaling launches replacement tasks in healthy AZs within an acceptable timeframe.

### 2. ECS health check grace period of zero may cause premature task termination
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ECS services (checkout-ngrh in us-east-1 and us-west-2) have HealthCheckGracePeriodSeconds set to 0. The checkout container has a 60-second StartPeriod for its health check, plus the init container must complete first. With zero grace period, the ALB target group health check may fail new tasks before they finish starting, causing ECS to terminate and replace them in a loop.
- **Mitigations:**
  - Set HealthCheckGracePeriodSeconds to at least 90 seconds on both ECS services (checkout-ngrh in us-east-1 and us-west-2) to account for the init container execution time plus the checkout container's 60-second StartPeriod and ALB target group registration time.
- **Observability:**
  - Monitor ECS service events for repeated task start/stop cycles on both checkout-ngrh services. Create a CloudWatch alarm on the service's RunningTaskCount dropping below DesiredCount for sustained periods.
- **Testing:**
  - Force a rolling replacement of all tasks in one region's checkout-ngrh service and observe whether new tasks successfully pass health checks and begin serving traffic without being prematurely terminated.

### 3. ALB target group lacks fail-open configuration risking total outage from coordinated health check failures
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have target groups with default minimum_healthy_targets settings (count=1, percentage=off). With cross-zone load balancing enabled and only 2 targets per region, a transient health check failure on both targets (e.g., from a dependency blip) would leave zero healthy targets, causing the ALB to return 503 errors to all requests.
- **Mitigations:**
  - Configure target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to 50 on both ALB target groups (apps-n-AlbTa-78XXUDCQAIGN in us-east-1 and apps-n-AlbTa-F8JMDIHZAMNF in us-west-2). This ensures the ALB continues routing traffic even when one of two targets fails health checks, preventing complete regional outage from coordinated health check failures.
- **Observability:**
  - Create CloudWatch alarms on HealthyHostCount for both target groups, alerting when the count drops below 2. Also monitor UnHealthyHostCount to detect coordinated health check failures early.
- **Testing:**
  - Use AWS FIS to inject network disruption to the Redis endpoint from ECS tasks, causing health checks to fail on all targets simultaneously. Verify that with fail-open configured, the ALB continues routing traffic rather than returning 503 errors.

### 4. No coordinated backup plan protecting S3 buckets against bulk deletion or ransomware
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** No AWS Backup plan exists to provide coordinated, centralized backup and recovery for the service's S3 buckets. Individual resource-level protections like versioning do not guard against scenarios such as ransomware encrypting all object versions, malicious bulk deletion, or compromised credentials wiping bucket contents across both regions simultaneously.
- **Mitigations:**
  - Create an AWS Backup plan that includes all three S3 buckets: basevpc-ngrh-canarybucket-o7lzn0tnthj6, basevpc-ngrh-canarybucket-vkxuke6ebamv, and region-switch-ngrh-reportsbucket-kgglku40vdri. Configure the plan with continuous backup for S3 to enable point-in-time recovery.
  - Enable AWS Backup Vault Lock on the backup vault to provide immutable backups that cannot be deleted even by compromised administrative credentials, protecting against ransomware scenarios.
  - Configure cross-region backup copy rules in the AWS Backup plan so that backups of each bucket are replicated to the opposite region, ensuring recoverability even if an entire region's backup vault is inaccessible.
- **Observability:**
  - Monitor AWS Backup CloudWatch metrics including NumberOfBackupJobsFailed and NumberOfRestoreJobsFailed. Create alarms that trigger on any backup job failure to ensure backup health is continuously validated.
- **Testing:**
  - Perform a quarterly restore drill by restoring one of the S3 buckets from the AWS Backup vault to a new bucket. Validate that the restored objects match expected content and that the restore completes within the 10-minute RTO target.

### 5. CloudWatch agent sidecar marked essential creates shared fate with checkout application
- **Severity:** MEDIUM
- **Category:** SHARED_FATE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** In both task definitions (us-east-1 and us-west-2), the ecs-cwagent (CloudWatch agent) container is marked as Essential: true. If the CloudWatch agent crashes or becomes unresponsive, ECS will terminate the entire task including the healthy checkout application container.
- **Mitigations:**
  - Change the ecs-cwagent container to Essential: false in both task definitions (apps-ngrh-CheckoutTaskDefinition-bFhdL3AYZfgy in us-east-1 and apps-ngrh-CheckoutTaskDefinition-nc93jm27q7Fi in us-west-2). This allows the checkout container to continue serving traffic even if the CloudWatch agent fails.
- **Observability:**
  - After making ecs-cwagent non-essential, create a CloudWatch alarm that detects gaps in telemetry data from the checkout service to alert when the CW agent sidecar has stopped functioning within a task.
- **Testing:**
  - Use AWS FIS with the ECS fault injection capability (enabled via EnableFaultInjection: true on the task definitions) to stop the ecs-cwagent container and verify the checkout container continues serving requests normally.

### 6. ALB long keep-alive delays connection redistribution from impaired nodes
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs (apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and apps-ngr-Alb-qjQDE37pXLaM in us-west-2) have client_keep_alive.seconds set to the default 3600 seconds. Upstream callers maintain persistent connections to ALB nodes for up to one hour, delaying connection redistribution when an ALB node or its AZ becomes impaired.
- **Mitigations:**
  - Reduce client_keep_alive.seconds to 180 on both ALBs (apps-ngr-Alb-sVmLp0H1caWm and apps-ngr-Alb-qjQDE37pXLaM). This ensures upstream clients re-establish connections more frequently, enabling faster redistribution away from impaired ALB nodes.
- **Observability:**
  - Monitor ALB ActiveConnectionCount and NewConnectionCount metrics per AZ for both ALBs to detect connection imbalances that could indicate impaired nodes holding stale connections.
- **Testing:**
  - Simulate an impaired ALB AZ by injecting network latency to one AZ's ALB nodes using AWS FIS, then measure how quickly upstream clients redistribute connections with the reduced keep-alive setting.

### 7. Route 53 accelerated recovery not enabled on hosted zone used for failover DNS
- **Severity:** LOW
- **Category:** SHARED_FATE
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The private hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io) has EnableAcceleratedRecovery set to false. If the us-east-1 Route 53 control plane is impaired and operators need to make manual DNS record changes during recovery, standard control-plane operations would be unavailable without accelerated recovery providing a fallback in us-west-2.
- **Mitigations:**
  - Enable Accelerated Recovery on hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io) to provide a fallback Route 53 control plane in us-west-2 if the us-east-1 control plane is unavailable, ensuring manual DNS changes remain possible during a regional impairment.
- **Observability:**
  - Monitor the Route 53 health check status changes for both health checks (<health-check-id> and <health-check-id>) and measure the elapsed time between health check failure and DNS record removal to baseline failover speed.
- **Testing:**
  - Execute the ARC Region Switch plan in a controlled test to measure end-to-end DNS failover time, including the DNS propagation component, and verify total recovery completes within the 10-minute RTO.

### 8. ALB deletion protection disabled risks accidental infrastructure removal
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have deletion_protection.enabled set to false. Accidental deletion of either ALB via console, CLI, or IaC would immediately sever all checkout traffic in that region.
- **Mitigations:**
  - Enable deletion protection on both ALBs (apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and apps-ngr-Alb-qjQDE37pXLaM in us-west-2) by setting the deletion_protection.enabled attribute to true.
- **Observability:**
  - Use AWS Config rules to continuously monitor that deletion protection remains enabled on both ALBs and alert if it is disabled.
- **Testing:**
  - Attempt to delete one of the ALBs in a non-production environment with deletion protection enabled to verify the protection prevents the operation.

### 9. ElastiCache Redis single-node per region with no failover or cross-region replication
- **Severity:** HIGH
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ElastiCache Redis replication groups run a single cache.t3.micro node in a single AZ with zero replicas, Multi-AZ disabled, and automatic failover disabled. There is no cross-region replication. Any node or AZ failure causes complete Redis unavailability in that region, and regional failover results in total loss of cart/session state.
- **Mitigations:**
  - Add at least one read replica to each ElastiCache replication group (apczqnhsypz3y0w in us-east-1 and apcxv15ao6wtn1k in us-west-2), placing the replica in a different AZ from the primary node.
  - Enable MultiAZEnabled and AutomaticFailoverEnabled on both replication groups to allow automatic promotion of the replica if the primary fails.
  - Enable ElastiCache Global Datastore to replicate Redis data from one region to the other for replication groups apczqnhsypz3y0w and apcxv15ao6wtn1k. This provides asynchronous cross-region replication so the surviving region has recent cart state after failover, reducing data loss toward the 5-minute RPO target.
  - If Redis state is treated as ephemeral and cart loss is acceptable, move cart state persistence to a cross-region replicated data store such as DynamoDB Global Tables. The checkout application would use DynamoDB instead of Redis for durable cart data, achieving RPO within the 5-minute target via asynchronous Global Tables replication.
- **Observability:**
  - Create CloudWatch alarms on the ReplicationLag, EngineCPUUtilization, and DatabaseMemoryUsagePercentage metrics for both replication groups. Alert when ReplicationLag exceeds a few seconds or when memory usage exceeds 80%.
  - Monitor the ElastiCache event notifications for failover events on both replication groups to track automatic failover occurrences.
  - If Global Datastore is enabled, create CloudWatch alarms on the ReplicationLag metric. Alert when replication lag exceeds 4 minutes (approaching the 5-minute RPO). Also monitor EngineCPUUtilization and CurrConnections on each cluster to detect saturation before failure.
- **Testing:**
  - After adding replicas, perform a failover test using the ElastiCache TestFailover API on each replication group to verify automatic promotion completes within acceptable time and the checkout application reconnects without manual intervention.
  - Execute an ARC Region Switch plan drill to deactivate one region and verify checkout behavior in the surviving region. Measure how many active carts are lost and whether the checkout service recovers gracefully without Redis state. If Global Datastore is implemented, verify that promoted Redis in the surviving region contains recent cart data within the RPO window.

### 10. S3 buckets lack versioning leaving canary artifacts and reports unprotected from accidental deletion
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The S3 buckets basevpc-ngrh-canarybucket-o7lzn0tnthj6 (us-east-1) and basevpc-ngrh-canarybucket-vkxuke6ebamv (us-west-2) do not have versioning enabled. These buckets store ALB access logs and canary artifacts. Without versioning, accidental deletion or overwrites of objects cannot be recovered.
- **Mitigations:**
  - Enable S3 versioning on bucket basevpc-ngrh-canarybucket-o7lzn0tnthj6 in us-east-1 and bucket basevpc-ngrh-canarybucket-vkxuke6ebamv in us-west-2.
  - Configure S3 lifecycle policies on both buckets to expire non-current object versions after a retention period appropriate for your audit and compliance requirements, preventing unbounded storage growth.
- **Observability:**
  - Monitor the S3 CloudWatch metric BucketSizeBytes for both canary buckets to detect unexpected growth from versioned objects accumulating. Set an alarm if bucket size exceeds a defined threshold.
- **Testing:**
  - Perform a restore test by deleting a non-critical test object from one of the canary buckets, then verify you can recover the previous version using S3 versioning. Confirm the restored object matches the original content.

## orders (10 findings)

### 1. Insufficient ECS task count risks major capacity loss from single AZ impairment
- **Severity:** HIGH
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Each region runs only 2 ECS tasks across 3 AZs. With 2 tasks in 3 AZs, at least one AZ has no task. If the AZ hosting a task experiences a gray impairment (elevated latency, partial errors while health checks pass), 50% of regional compute capacity is degraded. The ALB with cross-zone enabled continues routing to the impaired task. This degradation pattern may not be severe enough to trigger the ARC Region Switch alarms, leaving the service in a prolonged degraded state.
- **Mitigations:**
  - Increase the minimum desired count for the orders-ngrh ECS service from 2 to at least 3 in both regions, ensuring at least one task is placed in each AZ. Update autoScalingMinCapacity to 3 on arn:aws:ecs:us-east-1:123456789012:service/apps-ngrh-EcsCluster-5KPggqxgwMcr/orders-ngrh and arn:aws:ecs:us-west-2:123456789012:service/apps-ngrh-EcsCluster-kSVyjSlYruf7/orders-ngrh.
  - Verify that ECS AvailabilityZoneRebalancing (already ENABLED) distributes tasks evenly across all three AZs in each region. After scaling to 3 tasks, confirm one task is placed in each AZ.
- **Observability:**
  - Create CloudWatch alarms on per-AZ target group metrics for the orders target groups behind both ALBs. Monitor TargetResponseTime and HTTPCode_Target_5XX_Count with the AvailabilityZone dimension to detect AZ-localized degradation that may not trigger regional failover alarms.
  - Set up a CloudWatch alarm on the ECS service RunningTaskCount metric for orders-ngrh in both regions. Alert when running count drops below the minimum desired count, indicating potential AZ-related task placement failures.
- **Testing:**
  - Use AWS FIS with the aws:ecs:task-kill action targeting orders-ngrh tasks in a single AZ to validate that the remaining tasks absorb traffic without breaching latency or error rate thresholds. Verify that ECS replaces the killed task in a healthy AZ and that the ALB health checks correctly route around the gap.

### 2. No backup plan protects against logical data corruption replicating across both regions
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** No AWS Backup plan exists for the service's stateful components. Aurora DSQL's synchronous multi-region replication protects against infrastructure failures but not logical corruption, ransomware, or accidental bulk deletion, which would propagate to both regions simultaneously. S3 buckets also lack a unified backup strategy. Affected resources: DSQL clusters in us-east-1 and us-west-2, and S3 buckets basevpc-ngrh-canarybucket-o7lzn0tnthj6, basevpc-ngrh-canarybucket-vkxuke6ebamv, and region-switch-ngrh-reportsbucket-kgglku40vdri.
- **Mitigations:**
  - Create an AWS Backup plan that includes the Aurora DSQL clusters (arn:aws:dsql:us-east-1:123456789012:cluster/mzuey3whmen4jjmolnsx22iug4 and arn:aws:dsql:us-west-2:123456789012:cluster/ozuey3ui4fmwg56hgr2gpm2yz4) and all S3 buckets (basevpc-ngrh-canarybucket-o7lzn0tnthj6, basevpc-ngrh-canarybucket-vkxuke6ebamv, region-switch-ngrh-reportsbucket-kgglku40vdri). Configure appropriate backup frequency and retention based on business requirements.
  - Enable AWS Backup Vault Lock on the backup vault to provide immutable backups that protect against ransomware and accidental or malicious deletion of backup recovery points.
- **Observability:**
  - Create CloudWatch alarms on the AWS Backup metrics NumberOfBackupJobsFailed and NumberOfRestoreJobsFailed to detect backup or restore failures promptly. Alert when any backup job fails for the protected resources.
  - Monitor the AWS Backup metric NumberOfBackupJobsCompleted to confirm backups are running on schedule. Alert if no successful backup completes within the expected backup window.
- **Testing:**
  - Perform a quarterly restore test by restoring the Aurora DSQL backup to a separate cluster and validating data integrity against the production dataset. Measure restore duration to confirm it fits within the 10-minute RTO for data recovery scenarios.

### 3. S3 buckets lack versioning for protection against accidental deletion
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The S3 buckets basevpc-ngrh-canarybucket-o7lzn0tnthj6 (us-east-1) and basevpc-ngrh-canarybucket-vkxuke6ebamv (us-west-2) do not have versioning enabled. These buckets store ALB access logs. Without versioning, objects that are overwritten or deleted cannot be recovered. The region-switch reports bucket already has versioning enabled, indicating inconsistent configuration.
- **Mitigations:**
  - Enable S3 versioning on basevpc-ngrh-canarybucket-o7lzn0tnthj6 in us-east-1 and basevpc-ngrh-canarybucket-vkxuke6ebamv in us-west-2. Configure lifecycle rules to expire non-current object versions after an appropriate retention period to manage storage costs.
- **Observability:**
  - Use AWS Config rule s3-bucket-versioning-enabled to continuously monitor and alert if versioning is disabled on any S3 bucket in the service.
- **Testing:**
  - After enabling versioning, perform a test by deleting an object from one of the buckets and then restoring it from the previous version to confirm the versioning configuration works as expected.

### 4. Deletion protection disabled on critical ALBs and DSQL clusters
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both regional ALBs (apps-ngr-Alb-sVmLp0H1caWm and apps-ngr-Alb-qjQDE37pXLaM) and both Aurora DSQL clusters (mzuey3whmen4jjmolnsx22iug4 and ozuey3ui4fmwg56hgr2gpm2yz4) have deletion protection disabled. The ALBs are shared infrastructure serving multiple microservices, and the DSQL clusters store order data with RPO 0 requirements. Accidental deletion of any of these resources would cause a regional or complete service outage.
- **Mitigations:**
  - Enable deletion protection on ALB apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and ALB apps-ngr-Alb-qjQDE37pXLaM in us-west-2 by setting the deletion_protection.enabled attribute to true.
  - Enable deletion protection on DSQL cluster mzuey3whmen4jjmolnsx22iug4 in us-east-1 and DSQL cluster ozuey3ui4fmwg56hgr2gpm2yz4 in us-west-2 by setting DeletionProtectionEnabled to true.
- **Observability:**
  - Set up AWS Config rules to detect when deletion protection is disabled on ALB and DSQL cluster resources and trigger an alert via the critical-alarms SNS topics in each region.
- **Testing:**
  - After enabling deletion protection, attempt to delete the ALB and DSQL cluster via the AWS CLI and verify that the deletion protection prevents the operation with an appropriate error message.

### 5. RabbitMQ lacks cross-region replication risking event loss during regional failover
- **Severity:** MEDIUM
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** Each region's orders task references a region-local RabbitMQ instance. Amazon MQ for RabbitMQ has no native cross-region replication. If a region becomes impaired, the RabbitMQ broker in that region is lost along with any undelivered messages. After failover, the surviving region's RabbitMQ has no record of messages that were in-flight or queued in the impaired region, causing downstream notification loss for OrderCreatedEvent consumers.
- **Mitigations:**
  - Implement an outbox pattern where OrderCreatedEvent records are written to a table in Aurora DSQL within the same transaction as the order. Since DSQL provides synchronous multi-Region replication with RPO 0, the event records survive Regional failure. A poller or change-data-capture mechanism in each Region reads the outbox table and publishes to the local RabbitMQ broker, ensuring no events are lost after failover.
  - Accept the current design as-is given the assertion that lost MQ messages only affect downstream notifications and not order data integrity. Document this as an accepted risk with a reconciliation runbook that replays missed events from Aurora DSQL order records after a Regional failover.
- **Observability:**
  - Monitor the RabbitMQ broker's MessageCount and ConsumerCount metrics via CloudWatch in both regions. Alert when MessageCount exceeds a threshold indicating message backlog, or when ConsumerCount drops to zero, which could indicate the broker or consumers are unhealthy before a full Regional impairment is detected.
- **Testing:**
  - Conduct a Regional failover drill using the ARC Region Switch plan mr-rs-plan-ngrh. Before triggering failover, publish a known set of orders. After failover completes, verify that all orders are present in Aurora DSQL and quantify how many OrderCreatedEvent notifications were lost from the impaired Region's RabbitMQ. Validate that the reconciliation process can recover the missing events.

### 6. Route 53 accelerated recovery disabled delays DNS failover convergence
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The Route 53 hosted zone Z00581972E9UR4FTJIFDO (store.demo-ngrh.io) has EnableAcceleratedRecovery set to false. This feature reduces the time for DNS clients to detect and route away from unhealthy endpoints by shortening TTLs during health check failures. Without it, DNS propagation during failover takes longer. Additionally, if us-east-1 experiences an impairment, Route 53 control plane operations needed for recovery could be delayed.
- **Mitigations:**
  - Enable accelerated recovery on Route 53 hosted zone Z00581972E9UR4FTJIFDO (store.demo-ngrh.io). This feature automatically reduces DNS TTLs when health checks detect failures, enabling faster client convergence to healthy endpoints during regional failover, and ensures Route 53 control plane operations can be recovered in us-west-2 if us-east-1 is impaired.
- **Observability:**
  - Monitor Route 53 health check status metrics for both health checks (<health-check-id> for us-east-1 and <health-check-id> for us-west-2). Alert when either health check transitions to unhealthy to track DNS failover timing.
  - Monitor Route 53 control plane availability by tracking the success rate of Route 53 API calls from both regions. Alert if Route 53 API calls from us-east-1 fail consistently.
- **Testing:**
  - Execute the ARC Region Switch plan to deactivate one region and measure the end-to-end time from plan execution to when 100% of DNS queries resolve to the surviving region. Compare this with and without accelerated recovery enabled.
  - Test the activate workflow of the ARC Region Switch plan to confirm DNS restoration works correctly, validating that the full failover and failback cycle completes within the 10-minute RTO.

### 7. Gray AZ failures may cause prolonged degradation without triggering regional failover
- **Severity:** HIGH
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The ALBs have cross-zone load balancing enabled but no mechanism to detect and route around targets experiencing gray failures (elevated latency, intermittent errors) within a single AZ. The ARC Region Switch plan triggers on journey-level alarm conditions requiring corroboration from both regions, which may not activate for partial AZ degradation affecting only one region. Traffic continues flowing to impaired targets for the duration of the AZ event.
- **Mitigations:**
  - Configure the ALB target group health checks for the orders target groups to use shorter intervals (e.g., 5 seconds), lower healthy/unhealthy thresholds (e.g., 2 consecutive checks), and a health check path that exercises downstream dependencies like DSQL connectivity. This helps the ALB detect gray failures faster by failing health checks when the application is partially impaired.
  - Reduce the target group deregistration delay to a low value (e.g., 30 seconds) on both regional ALB target groups so that targets detected as unhealthy are removed from rotation quickly, minimizing the window of degraded responses.
  - Consider adding AZ-aware CloudWatch alarms that monitor per-AZ error rates and latency on the ALB target groups. Wire these alarms into an automated remediation (e.g., Lambda or Systems Manager automation) that can mark specific targets as unhealthy via the ELB API when AZ-localized degradation is detected, complementing the regional failover strategy for sub-regional impairments.
- **Observability:**
  - Create per-AZ CloudWatch alarms on the ALB target groups for both apps-ngr-Alb-sVmLp0H1caWm and apps-ngr-Alb-qjQDE37pXLaM. Use the AvailabilityZone dimension on TargetResponseTime (P99) and HTTPCode_Target_5XX_Count metrics. Alert when a single AZ shows latency or error rates significantly above peer AZs, indicating a gray failure that may not trigger regional failover.
  - Create a CloudWatch dashboard showing per-AZ health metrics for both regional ALBs side by side, enabling operators to quickly identify AZ-localized degradation and decide whether manual intervention is needed before regional failover triggers.
- **Testing:**
  - Use AWS FIS with the aws:az:power-interruption action targeting a single AZ in one region to simulate a full AZ impairment. Verify that the ARC Region Switch plan triggers correctly and completes failover within the 10-minute RTO. Measure the time from impairment start to full traffic shift to the surviving region.
  - Use AWS FIS with the aws:ecs:task-network-disruption action to inject latency or packet loss into orders-ngrh tasks in a single AZ, simulating a gray failure. Verify whether the ALB health checks detect the degradation, whether the regional failover alarms trigger, and measure the duration of customer-visible impact.

### 8. ECS health check grace period of zero causes premature task termination
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both orders-ngrh ECS services have healthCheckGracePeriodSeconds set to 0. The orders container is a Java/Spring Boot application with a container health check StartPeriod of 120 seconds, indicating significant initialization time. With a 0-second grace period, the ALB health check may mark newly launched tasks as unhealthy before the application is ready, causing ECS to terminate and replace them in a restart loop.
- **Mitigations:**
  - Set healthCheckGracePeriodSeconds to at least 150 seconds on both orders-ngrh ECS services (us-east-1 and us-west-2). This value should exceed the container's StartPeriod of 120 seconds plus the ALB target group health check registration time to allow the Java application to fully initialize before health evaluation begins.
- **Observability:**
  - Monitor ECS service events for repeated task start/stop cycles on both orders-ngrh services. Alert on the ECS service event pattern where tasks are stopped due to health check failures within the first 2 minutes of launch.
- **Testing:**
  - Force a rolling replacement of tasks in the orders-ngrh service and observe whether new tasks successfully pass health checks and begin serving traffic. Use AWS FIS action aws:ecs:stop-task to terminate a task and verify the replacement task starts successfully without entering a restart loop.

### 9. Shared ALB creates correlated failure risk across microservices via DNS failover
- **Severity:** MEDIUM
- **Category:** SHARED_FATE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs are shared infrastructure serving multiple microservices (carts, orders, assets, catalog, checkout, ui). An issue with any one service's target group (e.g., all targets unhealthy) can affect the ALB's DNS failover behavior, potentially impacting all other services behind the same ALB. With cross-zone load balancing enabled, a small target group breaching the DNS failover threshold could cause the ALB's IP to be removed from DNS for that AZ, affecting all services.
- **Mitigations:**
  - Configure the minimum_healthy_targets routing fail-open parameter on each target group behind the shared ALBs. Set target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to a value between 25-50% (since cross-zone is enabled) to prevent congestive collapse when health checks fail on a significant portion of the fleet.
  - Consider separating the orders service onto a dedicated ALB if the orders service has different criticality or significantly different fleet size compared to other services sharing the ALB. This eliminates the DNS-layer shared fate between services.
- **Observability:**
  - Monitor HealthyHostCount and UnHealthyHostCount per target group on both ALBs. Alert when the healthy host count for any target group drops below 50% of its registered targets, as this could trigger DNS failover affecting all co-located services.
- **Testing:**
  - Use AWS FIS to stop all tasks for a non-orders service sharing the ALB and observe whether the orders service target group experiences any disruption from the shared ALB's DNS failover behavior.

### 10. ALB keep-alive timeout delays client failover to healthy region
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** Both regional ALBs (apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and apps-ngr-Alb-qjQDE37pXLaM in us-west-2) have client_keep_alive.seconds set to the default 3600 seconds. During an impairment, clients hold persistent connections to the impaired ALB for up to one hour before re-establishing connections, prolonging the impact window and delaying DNS-based traffic shifting.
- **Mitigations:**
  - Set client_keep_alive.seconds to 180 on ALB apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and ALB apps-ngr-Alb-qjQDE37pXLaM in us-west-2. This forces clients to re-establish connections every 3 minutes, enabling faster convergence to healthy endpoints after DNS changes or impairments.
- **Observability:**
  - Monitor ALB ActiveConnectionCount and NewConnectionCount metrics for both ALBs. A sudden drop in new connections or a sustained high active connection count during an incident may indicate clients are stuck on stale connections.
- **Testing:**
  - Simulate a DNS traffic shift using the ARC Region Switch plan's activate/deactivate workflow and measure how quickly client traffic drains from the deactivated region's ALB. Compare drain times with the current 3600s setting versus the recommended 180s setting.

## assets (7 findings)

### 1. ALB deletion protection disabled risks accidental infrastructure loss
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs (apps-ngr-Alb-sVmLp0H1caWm in us-east-1 and apps-ngr-Alb-qjQDE37pXLaM in us-west-2) have deletion_protection.enabled set to false. These are shared ALBs serving multiple microservices. Accidental deletion would cause a complete regional outage for all services behind the ALB.
- **Mitigations:**
  - Enable deletion protection on the us-east-1 ALB apps-ngr-Alb-sVmLp0H1caWm by setting the deletion_protection.enabled attribute to true.
  - Enable deletion protection on the us-west-2 ALB apps-ngr-Alb-qjQDE37pXLaM by setting the deletion_protection.enabled attribute to true.
- **Observability:**
  - Create AWS Config rules to detect when deletion protection is disabled on ALB resources in both us-east-1 and us-west-2, and alert via the critical-alarms SNS topics when non-compliant.
- **Testing:**
  - Attempt to delete one of the ALBs via the console or CLI after enabling deletion protection to verify the safeguard prevents the operation.

### 2. ALB target groups lack fail-open configuration for small fleet
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have cross-zone load balancing enabled and their target groups use the default minimum_healthy_targets configuration (count=1, percentage=off). With only 2 targets per region, a transient health check failure on both targets simultaneously would cause the ALB to stop routing traffic entirely rather than failing open.
- **Mitigations:**
  - Configure target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to 50 on the assets target group (apps-n-AlbTa-78XXUDCQAIGN) in us-east-1, so the ALB fails open when fewer than 50% of targets are healthy.
  - Apply the same minimum_healthy_targets.percentage of 50 on the assets target group (apps-n-AlbTa-F8JMDIHZAMNF) in us-west-2.
- **Observability:**
  - Create CloudWatch alarms on the HealthyHostCount metric for both target groups, alerting when the count drops below 2 in either region. Use the critical-alarms SNS topics for notification.
- **Testing:**
  - Use AWS FIS with the aws:ecs:stop-task action to stop one ECS task in a region and verify the ALB continues serving traffic from the remaining healthy target. Then test with both targets stopped to confirm fail-open behavior routes traffic rather than returning 503 errors.

### 3. Shared ALB creates cross-service blast radius through DNS failover coupling
- **Severity:** MEDIUM
- **Category:** SHARED_FATE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs are shared infrastructure serving multiple microservices (assets, carts, orders, catalog, checkout, ui). If any one service's target group becomes unhealthy in an AZ, the ALB may remove its IP from DNS for that AZ, affecting all other services behind the same ALB including assets.
- **Mitigations:**
  - Consider separating services with different criticality levels or significantly different fleet sizes onto dedicated ALBs. At minimum, ensure all target groups behind each shared ALB have consistent minimum_healthy_targets DNS failover thresholds to prevent one service from triggering DNS removal for all services.
  - If separating ALBs is not feasible, configure target_group_health.dns_failover.minimum_healthy_targets.count to 1 on all target groups to prevent premature DNS removal triggered by a single service's health check failures.
- **Observability:**
  - Monitor HealthyHostCount per target group on both ALBs and create composite CloudWatch alarms that correlate unhealthy targets across different services to detect when one service's health issues risk triggering DNS failover for the shared ALB.
- **Testing:**
  - Simulate a target group becoming fully unhealthy for one non-assets service behind the shared ALB and verify that the assets service continues to receive traffic without interruption in the same AZ.

### 4. ARC Region Switch sequential step ordering exceeds 10-minute RTO
- **Severity:** HIGH
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The ARC Region Switch plan (mr-rs-plan-ngrh) executes three sequential steps: ECS scaling (15-min timeout), DNS traffic shift (5-min timeout), then Aurora switchover (10-min timeout). DNS traffic shift — the step that recovers the stateless assets service — cannot begin until ECS scaling completes. The scaling step alone has a 15-minute timeout, exceeding the 10-minute RTO before DNS shift starts.
- **Mitigations:**
  - Restructure the ARC Region Switch plan mr-rs-plan-ngrh to execute the Route 53 health check DNS shift step (shift-dns-traffic-away) either before or in parallel with the ECS scaling step (scale-up-ecs-services). This allows traffic to shift to the healthy Region immediately while capacity scales up concurrently, keeping total recovery time within the 10-minute RTO.
  - Reduce the ECS scaling execution block timeout from 15 minutes to a value that, combined with the DNS shift timeout, stays within the 10-minute RTO budget. Consider 5 minutes for scaling if it runs in parallel with DNS shift.
- **Observability:**
  - Monitor ARC Region Switch plan execution duration via the execution reports stored in s3://region-switch-ngrh-reportsbucket-kgglku40vdri/executions. Alert if total deactivate workflow duration exceeds 8 minutes (80% of the 10-minute RTO).
  - Track per-step execution times (scale-up-ecs-services, shift-dns-traffic-away, switch-over-catalog-db) to identify which step is the bottleneck during drills or real failovers.
- **Testing:**
  - Execute the ARC Region Switch plan mr-rs-plan-ngrh in a failover drill targeting each Region. Measure the wall-clock time from plan initiation to DNS traffic shift completion. Validate that the assets service recovers within the 10-minute RTO. Repeat after reordering the plan steps to confirm improvement.

### 5. ECS health check grace period of zero risks task replacement loops during scaling
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ECS services (assets-ngrh in us-east-1 and us-west-2) have HealthCheckGracePeriodSeconds set to 0. The container health check has a StartPeriod of 60 seconds, but the ALB target group health check may fail new tasks before the container is ready, causing ECS to terminate and replace tasks in a loop during scaling events or task replacements.
- **Mitigations:**
  - Set HealthCheckGracePeriodSeconds to 60 on the assets-ngrh ECS service in us-east-1 (cluster apps-ngrh-EcsCluster-5KPggqxgwMcr) to match the container health check StartPeriod.
  - Set HealthCheckGracePeriodSeconds to 60 on the assets-ngrh ECS service in us-west-2 (cluster apps-ngrh-EcsCluster-kSVyjSlYruf7) to match the container health check StartPeriod.
- **Observability:**
  - Monitor ECS service events for repeated task start-stop cycles by tracking the RunningTaskCount metric and alerting when it oscillates or remains below DesiredCount for more than 2 minutes on both regional services.
- **Testing:**
  - Trigger a scaling event by increasing the desired count on one regional ECS service and observe whether new tasks successfully pass ALB health checks and begin serving traffic without being prematurely terminated by ECS.

### 6. ALB long keep-alive delays client failaway from impaired load balancer nodes
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have client_keep_alive.seconds set to the default 3600 seconds (1 hour). When an ALB or its AZ becomes impaired, clients maintain persistent connections to the impaired load balancer node for up to one hour before re-establishing connections, prolonging the impact window.
- **Mitigations:**
  - Reduce client_keep_alive.seconds to 180 on the us-east-1 ALB apps-ngr-Alb-sVmLp0H1caWm.
  - Reduce client_keep_alive.seconds to 180 on the us-west-2 ALB apps-ngr-Alb-qjQDE37pXLaM.
- **Observability:**
  - Monitor ALB ActiveConnectionCount and NewConnectionCount metrics on both ALBs to observe connection cycling behavior and detect anomalies indicating clients are stuck on impaired nodes.
- **Testing:**
  - Simulate an AZ impairment using AWS FIS action aws:network:disrupt-connectivity targeting one AZ's subnets, then measure how quickly client connections redistribute to healthy ALB nodes with the reduced keep-alive setting versus the default.

### 7. Route 53 hosted zone lacks accelerated recovery for faster DNS failover
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The Route 53 hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io) has EnableAcceleratedRecovery set to false. This feature reduces DNS propagation time during failover events, which is critical for the ARC Region Switch plan's DNS traffic shift step.
- **Mitigations:**
  - Enable accelerated recovery on the Route 53 hosted zone Z00581972E9UR4FTJIFDO (demo-ngrh.io) to reduce DNS propagation latency during failover events.
- **Observability:**
  - Monitor the ARC Region Switch plan execution reports in the S3 bucket region-switch-ngrh-reportsbucket-kgglku40vdri to track how long the DNS shift step takes during failover drills and actual events.
- **Testing:**
  - Execute a controlled ARC Region Switch failover drill and measure the end-to-end time for the DNS shift step to complete and for clients to begin resolving to the surviving region's ALB.

## Notes for evaluators

- **cart finding 6 does not match the deployment.** NGRH says the carts table uses asynchronous replication that cannot meet an RPO of 0
  minutes. The table is created with `MultiRegionConsistency: STRONG` and a witness Region (`deployment/database/dynamodb/global-table.yml`),
  which replicates synchronously. Treat it as an assessor miss: a review that does not raise it is not missing a real gap, and a change
  to the table made only to close it would be wrong. It stays in the list because this file records what NGRH reported.
- The other findings describe settings the templates leave off or do not set (deletion protection, ALB keep-alive and fail-open, the ECS
  health check grace period, accelerated recovery) or choices the sample makes (two tasks per service, one ALB per Region for all services, a
  single-node Redis, a single-instance RabbitMQ broker).

## What changed since the previous baseline

The previous baseline (2026-06-17) had 35 findings: HIGH 5 · MEDIUM 20 · LOW 10. Since then the deployment and the model changed:

- **Compute**: every service runs on demand, so the 6 Fargate Spot findings (four of them HIGH) are gone.
- **Discovery**: each service finds its resources by tag instead of by stack, so a service's assessment covers its own and shared resources only.
- **Journeys**: cart is in all four journeys, so it is judged against all four.
- **Failover**: the plan scales up, moves DNS, then switches the catalog database over; it used to switch the database first, with a fallback that could lose data, and move DNS last. Eight alarm triggers were armed while the assessment ran.
- **Assertions**: 16 user assertions were in the template (the previous document counted 29), and Resilience Hub generated 27 more.
- **Health checks and deployments**: the services have container health checks, a shutdown delay and warm-up, which the previous baseline did not have.

NGRH does not say why a finding appears or goes away. The list above is what changed around the assessment, not a proven cause of
the difference in the counts. The "no assertions" comparison of the previous document (76 findings without assertions) was not repeated.

## Evaluation Criteria for Future Runs

### Improvement (findings decrease)

Each line is a change and the most findings it can remove, taken from the theme table. A change may remove fewer, because NGRH decides
anew on every run.

- **Deletion protection off (ALBs, Aurora, DSQL, DynamoDB table).** Turn deletion protection on for the ALBs, the Aurora clusters, the DSQL clusters and the carts table: up to 7 fewer findings (1 of them HIGH).
- **ALB client keep-alive left at the 3600 second default.** Set the ALB client keep-alive duration well below 3600 seconds: up to 6 fewer findings.
- **ALB target groups without fail-open.** Configure fail-open on every ALB target group: up to 4 fewer findings (1 of them HIGH).
- **Route 53 accelerated recovery off.** Enable accelerated recovery on the Route 53 hosted zone: up to 6 fewer findings.
- **ECS health check grace period of zero.** Give every ECS service a health check grace period: up to 6 fewer findings.
- **Two tasks per Region or two of three AZs used.** Run at least three tasks per service in each Region, across three AZs: up to 5 fewer findings (1 of them HIGH).
- **One ALB shared by several services.** Give each service its own load balancer: up to 3 fewer findings.
- **CloudWatch agent sidecar marked essential.** Do not mark the CloudWatch agent sidecar essential: up to 2 fewer findings.
- **Failover time or step timeouts against the 10-minute RTO.** Shorten or reorder the plan's steps so the configured timeouts fit the 10-minute RTO: up to 4 fewer findings (3 of them HIGH).
- **No AWS Backup plan or backup snapshots.** Add AWS Backup plans for Aurora, DynamoDB, DSQL and the S3 buckets: up to 4 fewer findings.
- **ElastiCache Redis without replicas or failover.** Give the checkout Redis a replica with automatic failover: up to 1 fewer finding (1 of them HIGH).
- **RabbitMQ without high availability or cross-Region replication.** Replicate the orders events across Regions or run the broker as a cluster: up to 1 fewer finding.
- **S3 buckets without versioning.** Turn on S3 versioning for the canary and report buckets: up to 2 fewer findings.
- **Gray AZ failures not handled.** Add checks that detect a partly impaired AZ: up to 2 fewer findings (1 of them HIGH).

### Regression (findings increase)

- New findings in previously clean areas indicate architecture degradation
- A total above 59 on an unchanged architecture, or a HIGH finding on a service that has none here (catalog), is a reason to
  re-run before calling it a regression
- A theme with more findings than its count in the theme table means a service or resource was added, or the run differed (re-run to confirm)
- Same finding appearing with higher severity language = possible LLM non-determinism (re-run to confirm)

### Expected Variation

- NGRH findings are **LLM-generated and non-deterministic**. Exact wording may vary across runs.
- Finding **counts** are expected to be more stable than finding **prose**, but this baseline cannot say by how much: one assessment per
  service was taken, so the run-to-run difference on an unchanged deployment has not been measured. The earlier text of this file (a
  difference of two in the total is normal) is not repeated, because an assessment of the same six services on 2026-07-16 listed 49 open
  findings against 35 on 2026-06-17.
- Finding IDs regenerate every assessment. Do not compare by ID.
- Recommendations are specific and actionable but may vary in phrasing across runs.
- Before treating a change in the total as real, assess an unchanged deployment twice and compare themes and severities.
