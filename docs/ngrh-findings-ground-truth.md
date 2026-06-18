# NGRH Findings Ground Truth

> Baseline assessment results for the AWS Resilience Hub (NGRH v2) model
> configured on this repository. Use this document to evaluate whether future
> assessment runs, agent-driven remediation, or architecture changes improve,
> regress, or maintain the resilience posture.

## Metadata

| Field | Value |
|-------|-------|
| Assessment date | 2026-06-17 |
| Account | 968715863728 |
| Stack | ngrh-ngrh |
| Services | 6 (ui, catalog, cart, checkout, orders, assets) |
| Policies | TierOne (RTO=10 min, RPO=0) · TierTwo (RTO=10 min, RPO=5 min) |
| Tier1 services | ui, cart, orders |
| Tier2 services | catalog, checkout, assets |
| Total findings | 35 |
| Assertions active | 29 (suppress known-acceptable patterns) |
| Reduction from baseline (no assertions) | 54% (76 → 35) |

## Architecture Context

- **Deployment**: Active/Active, us-east-1 (primary) + us-west-2 (standby)
- **Compute**: ECS Fargate (FARGATE_SPOT only — design choice for cost, not production recommendation)
- **ARC Region Switch**: Automated triggers via composite alarm (opt-in via `ENABLE_TRIGGERS=true`)
- **Pre-scaling**: 200% of containerInsightsMaxInLast24Hours before failover proceeds
- **Data**: Aurora Global DB (catalog), DynamoDB Global Table (cart), Aurora DSQL (orders), ElastiCache Redis (checkout, ephemeral), RabbitMQ (orders, SINGLE_INSTANCE)

## Findings

## ui (6 findings)

### 1. ECS services use FARGATE_SPOT exclusively risking capacity loss during failover
- **Severity:** HIGH
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both UI ECS services (ui-ngrh in us-east-1 and us-west-2) use FARGATE_SPOT as the sole capacity provider with weight=1 and base=0. 100% of tasks run on interruptible Spot capacity. During regional failover requiring 200% scale-up, Spot capacity may be unavailable. Spot interruptions can also terminate all tasks simultaneously during capacity shortages.
- **Mitigations:**
  - Change the CapacityProviderStrategy for ui-ngrh services in both regions to use FARGATE (on-demand) as the base capacity provider with a base of at least 2, and FARGATE_SPOT as a weighted overflow. This ensures minimum capacity is always available on non-interruptible infrastructure.
  - Alternatively, switch entirely to FARGATE (on-demand) capacity provider for the UI services given their critical role as the entry point for all user traffic. The cost increase is justified by the 99.99% availability requirement.
- **Observability:**
  - Create CloudWatch alarms on ECS RunningTaskCount for ui-ngrh in both us-east-1 and us-west-2, alerting when it drops below the desired count for 60 seconds. Additionally, create an EventBridge rule to capture Fargate Spot interruption events and publish to SNS for early warning of capacity reclamation.
  - Create CloudWatch alarms comparing ECS RunningTaskCount vs DesiredTaskCount for ui-ngrh in both regions. Alert when running count is below desired count for more than 60 seconds, indicating task placement failures from Spot capacity unavailability.
- **Testing:**
  - Use AWS FIS action aws:ecs:stop-task to terminate UI tasks and verify the service recovers within acceptable bounds. Measure time-to-recovery when tasks are stopped and confirm new tasks launch successfully.
  - Execute the ARC Region Switch plan mr-rs-plan-ngrh in a test deactivation of one region and verify that the surviving region's ui-ngrh service successfully scales to 200% within the 15-minute timeout. Measure actual task placement time and confirm all tasks reach RUNNING state.

### 2. ALB client keep-alive at default 3600 seconds delays recovery from impaired nodes
- **Severity:** LOW
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have client_keep_alive.seconds set to 3600 (default). Clients maintain persistent connections to ALB nodes for up to 1 hour, meaning if an ALB node or its AZ becomes impaired, clients continue sending requests to the impaired path for up to an hour before re-resolving DNS.
- **Mitigations:**
  - Reduce client_keep_alive.seconds to 180 on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2). This ensures clients reconnect every 3 minutes, limiting exposure to impaired ALB nodes while keeping connection reuse benefits for the medium traffic volume.
- **Observability:**
  - Create CloudWatch alarms on ActiveConnectionCount and NewConnectionCount for both ALBs (apps-ngr-Alb-c29ylkrIPFgn and apps-ngr-Alb-AHlnhinNoTnn). Alert if NewConnectionCount exceeds a baseline by more than 3 standard deviations using anomaly detection, indicating connection churn after keep-alive changes.
- **Testing:**
  - After reducing keep-alive, run a load test at expected peak traffic to confirm the increased connection establishment rate does not cause latency degradation or connection errors. Measure p99 latency before and after the change.

### 3. Route 53 hosted zone lacks accelerated recovery slowing DNS failover propagation
- **Severity:** LOW
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The hosted zone Z06826382CMBA78V1ZKL6 has EnableAcceleratedRecovery set to false. This feature reduces DNS propagation time during failover events, which is critical when the ARC Region Switch plan shifts DNS traffic away from an impaired region.
- **Mitigations:**
  - Enable EnableAcceleratedRecovery on hosted zone Z06826382CMBA78V1ZKL6. This reduces DNS propagation delays during failover events triggered by the ARC Region Switch plan, helping meet the 10-minute RTO target.
- **Observability:**
  - Create CloudWatch alarms on Route 53 HealthCheckStatus metric for each health check associated with hosted zone Z06826382CMBA78V1ZKL6. Track HealthCheckPercentageHealthy and alert on state transitions. During failover drills, measure time from health check failure to DNS query pattern shift using Route 53 query logging.
- **Testing:**
  - Execute the ARC Region Switch plan in a controlled failover drill and measure end-to-end DNS propagation time. Compare propagation latency with and without accelerated recovery to quantify the improvement.

### 4. ALB deletion protection disabled on production load balancers
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both production ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2) have deletion_protection.enabled set to false. Accidental deletion of either ALB via console, CLI, or IaC misconfiguration would immediately cause complete service unavailability in that region.
- **Mitigations:**
  - Enable deletion_protection.enabled on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2). This prevents accidental deletion via API/CLI/console without first explicitly disabling the protection.
- **Observability:**
  - Deploy an AWS Config rule (e.g., elb-deletion-protection-enabled) targeting both production ALBs to continuously verify deletion protection is enabled. Configure remediation or SNS notification on non-compliant findings.
- **Testing:**
  - Verify deletion protection by attempting to delete the ALB via CLI in a non-production environment with the same configuration, confirming the API returns an error when protection is enabled.

### 5. ALB target groups lack minimum healthy targets threshold risking congestive collapse
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both regional target groups have target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage set to 'off' and count set to 1. With cross-zone load balancing enabled and only 2 targets, if health checks fail on one target due to a transient issue, all traffic concentrates on the single remaining target, risking congestive collapse.
- **Mitigations:**
  - Set target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to 50 on both target groups. This causes the ALB to fail open and distribute traffic to all targets when more than half are marked unhealthy, preventing congestive collapse from coordinated health check failures.
- **Observability:**
  - Create CloudWatch alarms on HealthyHostCount for both target groups (apps-n-AlbTa-N5B8VPEB42KO and apps-n-AlbTa-YAIU6ZYVARN6), alerting when healthy hosts drop below 2. Also create alarms on UnHealthyHostCount > 0 to detect coordinated health check failures early.
- **Testing:**
  - Inject a fault that causes the /actuator health check to fail on all targets simultaneously (e.g., by blocking connectivity to a downstream dependency) and verify the ALB fails open rather than dropping all traffic. Confirm requests continue to be served during the transient failure.

### 6. UI targets span only 2 of 3 AZs risking 50% capacity loss during AZ impairment
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The UI target groups in both regions show targets registered in only 2 AZs each (us-west-2b/us-west-2c and us-east-1d/us-east-1a). With only 2 tasks per region, a single AZ impairment removes 50% of UI capacity. Starting from 2 tasks during regional failover means significant scale-up latency to reach the needed capacity.
- **Mitigations:**
  - Increase the minimum capacity for the ui-ngrh auto-scaling targets in both regions from 2 to at least 3 tasks. With 3 tasks and AZ rebalancing enabled, ECS will distribute tasks across all 3 configured subnets, ensuring no single AZ failure removes more than one-third of capacity and providing better baseline for absorbing failover traffic.
  - Alternatively, pre-provision both regions at a higher baseline (e.g., 4 tasks each) so that during failover the surviving region already has sufficient capacity to handle combined traffic while additional scaling occurs.
- **Observability:**
  - Create CloudWatch alarms on HealthyHostCount with AvailabilityZone dimension for both ALB target groups (apps-n-AlbTa-N5B8VPEB42KO in us-east-1 and apps-n-AlbTa-YAIU6ZYVARN6 in us-west-2). Alert when any AZ has zero healthy targets.
  - Create CloudWatch alarms on TargetResponseTime (p99 > 2s) and HTTPCode_Target_5XX_Count (> 1% of requests) for both target groups, evaluated over 2 minutes, to detect capacity pressure from AZ loss.
- **Testing:**
  - Use AWS FIS with the aws:az:power-interruption action targeting one AZ to validate that the remaining UI tasks can handle regional load and that the ARC Region Switch triggers correctly if the impairment persists.
  - Conduct a load test simulating 200% of normal traffic against a single region's ALB while measuring response times and error rates. Verify that auto-scaling responds within the 10-minute RTO window.


## catalog (9 findings)

### 1. ARC Region Switch plan sequential execution risks exceeding 10-minute RTO
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The ARC Region Switch plan (mr-rs-plan-ngrh) executes ECS scaling (up to 15-minute timeout) BEFORE Aurora Global Database failover (up to 20-minute timeout), followed by DNS shift (5-minute timeout). These steps are sequential, meaning total worst-case execution time far exceeds the 10-minute RTO. Even under normal conditions, the sum of ECS scaling time plus Aurora promotion time plus DNS shift approaches or exceeds the RTO budget.
- **Mitigations:**
  - Restructure the ARC Region Switch plan to execute the Aurora Global Database failover step in parallel with ECS scaling rather than sequentially. Since both are independent operations, parallelizing them reduces total recovery time to the duration of the longest single step rather than the sum of all steps.
  - Reduce the TimeoutMinutes for the ECS scaling execution block from 15 to a value closer to observed scaling time (e.g., 5 minutes) to fail fast if scaling is not progressing, and reduce the Aurora failover timeout from 20 to 5 minutes since normal promotion completes in 1-2 minutes.
- **Observability:**
  - Monitor the ARC Region Switch plan execution duration via CloudWatch metrics for the mr-rs-plan-ngrh plan. Set an alarm if total plan execution time exceeds 8 minutes to provide early warning of RTO breach risk.
- **Testing:**
  - Execute the ARC Region Switch plan mr-rs-plan-ngrh in a failover drill targeting us-east-1 deactivation. Measure end-to-end time from plan initiation to DNS shift completion. Success criteria: total execution completes within 10 minutes. Use AWS FIS action aws:arc:initiate-region-switch to trigger the plan and measure recovery time.

### 2. ALB default keep-alive delays recovery from impaired load balancer nodes
- **Severity:** LOW
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have client_keep_alive.seconds set to the default 3600 (1 hour). Clients maintain persistent connections to load balancer nodes for up to an hour, delaying connection redistribution if a load balancer node or its AZ becomes impaired.
- **Mitigations:**
  - Reduce client_keep_alive.seconds to 180 on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2). This balances connection reuse efficiency with faster recovery from impaired load balancer nodes.
- **Observability:**
  - Monitor ALB ActiveConnectionCount and NewConnectionCount metrics for both load balancers. Alert on sustained elevated 5xx error rates which may indicate impaired nodes. A sudden spike in NewConnectionCount after reducing keep-alive confirms clients are cycling connections as expected.
- **Testing:**
  - After reducing the keep-alive value, run a load test and verify that connection establishment overhead does not materially increase latency. Monitor p99 response times to confirm no regression from more frequent connection cycling.

### 3. ALB deletion protection disabled risks accidental infrastructure loss
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have deletion_protection.enabled set to false. Accidental deletion of either load balancer would immediately disrupt all traffic in that region, affecting the catalog service and all other microservices sharing the ALB (ui, carts, checkout, orders, assets).
- **Mitigations:**
  - Enable deletion_protection on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2) by setting the deletion_protection.enabled attribute to true.
- **Observability:**
  - Use AWS Config rules to continuously monitor that deletion_protection.enabled remains true on both ALBs. Alert immediately if the setting is changed to false.
- **Testing:**
  - Attempt to delete the ALB via CLI with deletion protection enabled and verify the operation is rejected with an appropriate error message.

### 4. Catalog service directs all reads to writer endpoint wasting reader capacity
- **Severity:** LOW
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both catalog task definitions set DB_ENDPOINT and DB_READ_ENDPOINT to the same SSM parameter value (the cluster writer endpoint). All read traffic goes to the writer instance rather than being distributed across the reader endpoint, leaving the reader replica underutilized and halving available read capacity for this read-heavy workload.
- **Mitigations:**
  - Update the SSM parameter referenced by DB_READ_ENDPOINT (or create a separate parameter) to point to the Aurora cluster reader endpoint: catalog-dbcluster-01-us-east-1-ngrh.cluster-ro-c07i4aqkkxsb.us-east-1.rds.amazonaws.com in us-east-1 and catalog-dbcluster-02-us-west-2-ngrh.cluster-ro-c7owewgw69l9.us-west-2.rds.amazonaws.com in us-west-2. Then redeploy the catalog ECS tasks to pick up the new endpoint.
- **Observability:**
  - Add per-instance CloudWatch alarms comparing writer vs reader CPUUtilization. Alert if the writer instance CPUUtilization exceeds 70% while the reader remains below 20%, indicating read traffic is not being distributed to reader instances.
- **Testing:**
  - After splitting read traffic to the reader endpoint, run a load test simulating peak catalog read traffic. Verify that connections are distributed across both instances and that p99 latency improves compared to the single-endpoint configuration.

### 5. ECS catalog targets in only 2 AZs risks 50% capacity loss during AZ impairment
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The ALB target groups in both regions show targets registered in only 2 AZs each (us-east-1: us-east-1d and us-east-1a; us-west-2: us-west-2b and us-west-2c). With only 2 tasks per region spread across 2 AZs, a single AZ impairment removes 50% of serving capacity in that region, potentially overwhelming the remaining task before the regional failover triggers.
- **Mitigations:**
  - Increase the DesiredCount for the catalog-ngrh ECS service in both regions from 2 to at least 3 tasks. This ensures Fargate distributes tasks across all 3 configured subnets/AZs, so losing one AZ removes only ~33% of capacity rather than 50%. With AvailabilityZoneRebalancing already enabled, ECS will maintain balanced distribution.
- **Observability:**
  - Monitor per-AZ target health and request count on both ALB target groups (apps-n-AlbTa-N5B8VPEB42KO in us-east-1 and apps-n-AlbTa-YAIU6ZYVARN6 in us-west-2). Create CloudWatch alarms on HealthyHostCount dropping below 2 per target group and on per-AZ 5XX rates exceeding 5% to detect AZ-scoped degradation early.
- **Testing:**
  - Use AWS FIS with the aws:ecs:stop-task action targeting catalog-ngrh tasks in a single AZ to simulate AZ capacity loss. Verify that the remaining tasks absorb traffic without exceeding acceptable latency thresholds and that the regional failover alarm triggers appropriately if degradation persists.

### 6. Aurora Global Database lacks immutable backup protection against catastrophic deletion
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The Aurora Global Database clusters have native automated backups with 7-day retention, but no AWS Backup plan provides isolated, immutable backup copies. DeletionProtection is disabled on the global cluster and both regional clusters. A compromised IAM principal or IaC misconfiguration could delete both clusters and their native backups simultaneously. Affected resources: catalog-global-db-cluster-ngrh, catalog-dbcluster-01-us-east-1-ngrh, catalog-dbcluster-02-us-west-2-ngrh, and all four DB instances.
- **Mitigations:**
  - Create an AWS Backup plan that includes both Aurora clusters (catalog-dbcluster-01-us-east-1-ngrh and catalog-dbcluster-02-us-west-2-ngrh) with continuous backup enabled for point-in-time recovery. Store backups in a vault with vault lock policy to prevent deletion. Configure cross-region copy rules so backups exist independently of the source clusters.
  - Enable DeletionProtection on the Aurora Global Cluster (catalog-global-db-cluster-ngrh), both regional clusters (catalog-dbcluster-01-us-east-1-ngrh and catalog-dbcluster-02-us-west-2-ngrh), and all DB instances.
- **Observability:**
  - Create CloudWatch alarms on the AWS Backup metrics aws/backup CopyJobFailed and BackupJobFailed for the Aurora resources. Alert when any backup or copy job fails. Monitor the backup:RecoveryPointCompleted event to confirm continuous backup recovery points are being created for both clusters.
  - Use AWS Config rule rds-cluster-deletion-protection-enabled to continuously monitor that deletion protection remains enabled on all Aurora clusters.
- **Testing:**
  - Perform a quarterly restore test by restoring the Aurora cluster from an AWS Backup recovery point to a new cluster in a test environment. Validate that the restored data is consistent and that the catalog application can connect and serve requests. Measure restore time to confirm it fits within the 10-minute RTO when combined with the ARC Region Switch automation.
  - After enabling deletion protection, attempt to delete a DB cluster via CLI and verify the operation is rejected.

### 7. ALB target group lacks fail-open protection against coordinated health check failures
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both regional target groups have minimum_healthy_targets set to 'off'/count=1. With cross-zone load balancing enabled and only 2 targets per region, a coordinated health check failure (e.g., from a transient Aurora connectivity blip affecting the /actuator deep health check) could mark both targets unhealthy, causing all traffic to be rejected before fail-open activates.
- **Mitigations:**
  - Set target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to 50 on both target groups (apps-n-AlbTa-N5B8VPEB42KO in us-east-1 and apps-n-AlbTa-YAIU6ZYVARN6 in us-west-2). With cross-zone enabled and 2 targets, this ensures the ALB fails open and continues routing traffic if more than half the targets fail health checks simultaneously, preventing congestive collapse from transient dependency issues.
- **Observability:**
  - Create CloudWatch alarms on the ALB TargetGroup metrics UnHealthyHostCount and HealthyHostCount for both target groups. Alert when UnHealthyHostCount exceeds 0 for more than 60 seconds, and critically alert when HealthyHostCount drops to 0.
- **Testing:**
  - Simulate a transient database connectivity failure using AWS FIS to temporarily block network traffic between the ECS tasks and Aurora. Verify that the ALB fails open rather than rejecting all traffic, and measure the duration of any request failures.

### 8. Aurora cluster lacks read capacity redundancy after AZ failover
- **Severity:** MEDIUM
- **Category:** SHARED_FATE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** In us-east-1, both Aurora instances are in us-east-1c and us-east-1d (2 AZs). In us-west-2, both instances are in us-west-2b and us-west-2c (2 AZs). After an AZ impairment triggers Aurora failover, the cluster operates with a single instance until the impaired AZ recovers. For a read-heavy catalog workload, the single remaining instance must handle all read traffic.
- **Mitigations:**
  - Add a third Aurora Serverless v2 reader instance in a third AZ for each cluster (us-east-1a for catalog-dbcluster-01-us-east-1-ngrh, and us-west-2a for catalog-dbcluster-02-us-west-2-ngrh). This ensures that after an AZ impairment and automatic failover, at least two instances remain available to serve read traffic.
- **Observability:**
  - Add per-instance CloudWatch alarms on CPUUtilization for each Aurora instance (catalog-dbcluster-01-ngrh-1, catalog-dbcluster-01-ngrh-2, catalog-dbcluster-02-ngrh-1, catalog-dbcluster-02-ngrh-2). Alert when a single instance's CPUUtilization exceeds 70% for 2 consecutive periods, indicating the other instance may be unavailable due to AZ impairment.
- **Testing:**
  - Perform an Aurora failover drill on catalog-dbcluster-01-us-east-1-ngrh using the RDS console or CLI failover command. Measure the time for the catalog ECS service to reconnect and resume serving requests, and verify that the application handles the brief connection interruption gracefully without triggering the regional failover alarm.

### 9. ECS services use FARGATE_SPOT exclusively risking capacity loss during failover
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** All ECS services (catalog-ngrh and ui-ngrh) in both regions use FARGATE_SPOT with Weight=1 and Base=0, meaning 100% of tasks run on interruptible Spot capacity. During a regional impairment, the surviving region must absorb 100% of traffic via the ARC Region Switch plan scaling to 200%, but Spot capacity may be unavailable precisely when demand surges due to widespread customer failovers competing for Spot capacity. Spot tasks can also be interrupted with 2-minute warning during normal operations.
- **Mitigations:**
  - Add a FARGATE (on-demand) capacity provider with a Base value equal to the minimum task count needed to serve full production traffic (e.g., Base: 4 for catalog-ngrh) alongside FARGATE_SPOT for burst capacity. This ensures a guaranteed baseline of tasks that cannot be interrupted, while still using Spot for cost optimization above the baseline.
  - Modify the CapacityProviderStrategy for catalog-ngrh ECS services in both regions to include FARGATE as a base capacity provider with Base=2 (matching the minimum task count), and keep FARGATE_SPOT with Weight=1 for scale-out tasks.
- **Observability:**
  - Monitor the CapacityProviderReservation metric for FARGATE_SPOT on clusters apps-ngrh-EcsCluster-Gajk5ANQWU57 and apps-ngrh-EcsCluster-DfCQjH7kK2Bh. Alert when the service's RunningTaskCount is below DesiredCount for more than 2 minutes, indicating Spot capacity shortfall.
  - Monitor Fargate Spot interruption events via EventBridge to detect capacity reclamation patterns.
- **Testing:**
  - Execute the ARC Region Switch plan to deactivate one Region and verify that the surviving Region's ECS services successfully scale to 200% within the expected timeframe. Measure time from scale request to all tasks reaching RUNNING state. Success criteria: all tasks running within 5 minutes of the scaling step initiation.
  - Use AWS FIS action aws:ecs:stop-task to terminate catalog tasks and verify the service recovers within acceptable latency bounds.


## cart (6 findings)

### 1. DynamoDB Global Table lacks deletion protection enabling accidental data loss
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The DynamoDB Global Table cartsTable-ngrh has DeletionProtectionEnabled set to false on both the us-east-1 and us-west-2 replicas. A single API call could delete the table across both regions simultaneously, destroying the sole persistent data store for the carts service with no failover possible.
- **Mitigations:**
  - Enable DeletionProtectionEnabled on both replicas (us-east-1 and us-west-2) of the DynamoDB Global Table cartsTable-ngrh. This prevents accidental or unauthorized deletion of the table and should be applied via the infrastructure-as-code template to ensure it persists across deployments.
- **Observability:**
  - Create a CloudWatch alarm on CloudTrail events for DeleteTable API calls targeting cartsTable-ngrh. Alert immediately on any attempt, whether successful or denied, to detect potential malicious activity or misconfiguration. Use AWS Config to continuously monitor the DeletionProtectionEnabled setting on both replicas and alert on non-compliant changes.
- **Testing:**
  - After enabling deletion protection, attempt a DeleteTable API call against cartsTable-ngrh in a non-production environment to verify the protection correctly rejects the request with a ValidationException. Validate that the protection applies to both regional replicas independently.

### 2. ALB client keep-alive at default 3600 seconds delays impaired-node drainage
- **Severity:** LOW
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have client_keep_alive.seconds set to 3600 (default). Clients maintain persistent connections to load balancer nodes for up to 1 hour, even if those nodes are in an impaired AZ or experiencing degraded performance, delaying natural traffic redistribution.
- **Mitigations:**
  - Reduce client_keep_alive.seconds from 3600 to 180 on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2). This ensures clients cycle connections more frequently, reducing exposure to impaired load balancer nodes while maintaining reasonable connection reuse efficiency.
- **Observability:**
  - Add CloudWatch monitoring on ALB ActiveConnectionCount and NewConnectionCount for both load balancers (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2). After reducing keep-alive timeout, set alarms if NewConnectionCount spikes abnormally or TargetResponseTime p99 increases, indicating connection re-establishment overhead.
- **Testing:**
  - After changing the keep-alive setting, run a load test to verify that the increased connection churn does not materially impact latency percentiles (p99) or error rates. Compare baseline metrics before and after the change.

### 3. ALB deletion protection disabled risking accidental infrastructure removal
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have deletion_protection.enabled set to false. Accidental deletion of either load balancer via console, CLI, or IaC misconfiguration would immediately disrupt all traffic in that region, affecting the carts service and all other microservices sharing the ALB.
- **Mitigations:**
  - Enable deletion_protection on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2) by setting the deletion_protection.enabled attribute to true.
- **Observability:**
  - Deploy an AWS Config rule (e.g., elb-deletion-protection-enabled) to continuously evaluate that deletion_protection.enabled is true on both ALBs. Configure a CloudWatch alarm on Config compliance status changes to alert immediately when either ALB becomes non-compliant.
- **Testing:**
  - After enabling deletion protection, attempt to delete the ALB via CLI to confirm the operation is blocked with an appropriate error message. This validates the safeguard is functioning correctly.

### 4. ALB Target Groups lack fail-open routing during coordinated health check failures
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALB Target Groups have minimum_healthy_targets.count set to 1 and percentage set to 'off'. With only 2 targets per Target Group, a coordinated health check failure (e.g., transient dependency issue on /actuator endpoint) could mark both targets unhealthy, causing the ALB to return 503s to all clients.
- **Mitigations:**
  - Set target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to 50 on both Target Groups (apps-n-AlbTa-N5B8VPEB42KO in us-east-1 and apps-n-AlbTa-YAIU6ZYVARN6 in us-west-2). This ensures the ALB fails open and continues routing traffic when health checks experience coordinated failures, rather than dropping all requests.
- **Observability:**
  - Add CloudWatch alarms on ALB HealthyHostCount for target groups apps-n-AlbTa-N5B8VPEB42KO (us-east-1) and apps-n-AlbTa-YAIU6ZYVARN6 (us-west-2). Alert when HealthyHostCount drops to 1 or below. Also add alarms on HTTPCode_ELB_5XX_Count for both ALBs to detect when no healthy targets are available and the ALB is returning 5xx errors.
- **Testing:**
  - Inject a fault that causes the /actuator health check endpoint to return 500 on all targets simultaneously (e.g., by blocking connectivity to a dependency the health check validates). Verify that with the fail-open configuration, the ALB continues routing traffic rather than returning 503 errors to all clients.

### 5. ECS carts services rely exclusively on FARGATE_SPOT risking capacity loss
- **Severity:** HIGH
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both carts ECS services in us-east-1 and us-west-2 use FARGATE_SPOT with Weight=1 and Base=0 as their sole capacity provider. During Spot capacity shortages, all tasks (DesiredCount=2 per region) can be interrupted simultaneously with only 2-minute notice, and replacement tasks also depend on Spot availability.
- **Mitigations:**
  - Change the CapacityProviderStrategy for carts-ngrh services in both regions to use a Base of at least 2 on FARGATE (on-demand) with FARGATE_SPOT as the weighted overflow. This ensures a minimum baseline of tasks always runs on reliable on-demand capacity while still benefiting from Spot cost savings for burst capacity.
  - Alternatively, switch entirely to FARGATE on-demand capacity provider for the carts services given the 99.99% availability requirement. The cost increase is justified by eliminating Spot interruption risk for this critical PRIMARY service function.
- **Observability:**
  - Add a CloudWatch alarm on ECS Service RunningTaskCount for carts-ngrh in both us-east-1 and us-west-2. Alert when RunningTaskCount drops below DesiredCount for more than 60 seconds, indicating Spot interruptions or placement failures. Additionally, create an EventBridge rule to capture ECS task state change events with STOPPED reason containing 'spot interruption' and route to SNS for immediate notification.
- **Testing:**
  - Execute the ARC Region Switch plan (mr-rs-plan-ngrh) in a drill to validate that the ECS scaling execution block completes within its 15-minute timeout. Observe whether Fargate Spot tasks launch successfully during the scale-up to 200%. Use AWS FIS action aws:ecs:stop-task to simulate task interruptions during the scaling event and measure time-to-recovery.

### 6. No AWS Backup plan protecting DynamoDB table against correlated data destruction
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The DynamoDB Global Table cartsTable-ngrh has PITR enabled but no AWS Backup plan. PITR operates within the same account and can be disabled by a compromised principal. Without vault-locked backups, a destructive event affecting both regions simultaneously could result in permanent data loss.
- **Mitigations:**
  - Create an AWS Backup plan that includes the DynamoDB Global Table cartsTable-ngrh. Configure the plan with snapshot-based backups at a frequency appropriate to the business (e.g., daily or more frequent). Store backups in a vault with AWS Backup Vault Lock enabled to prevent deletion. Consider cross-account backup copies for additional isolation against account compromise.
- **Observability:**
  - Add CloudWatch alarms on AWS Backup metrics NumberOfBackupJobsCompleted and NumberOfBackupJobsFailed for cartsTable-ngrh. Alert when backup jobs fail (NumberOfBackupJobsFailed > 0) or when no successful backup completes within the expected schedule window (NumberOfBackupJobsCompleted = 0 over the backup interval period).
- **Testing:**
  - Perform a periodic restore test of cartsTable-ngrh from AWS Backup to a new table name. Validate that the restored data is consistent and that the restore completes within the expected RTO. Measure restore duration and verify item counts match expectations.


## checkout (6 findings)

### 1. Checkout session state lost on regional failover with no cross-region replication
- **Severity:** MEDIUM
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The checkout service uses independent Redis instances per region with no cross-region replication (confirmed by assertion). When traffic shifts away from an impaired region via ARC Region Switch, all in-flight checkout sessions stored in that region's Redis are permanently lost. Users mid-checkout must restart the checkout flow.
- **Mitigations:**
  - Replace the independent ElastiCache replication groups (apche2zq6d0u0yh in us-east-1 and apctm8wm9gg5e7j in us-west-2) with an ElastiCache Global Datastore configuration. This provides asynchronous cross-region replication of session state, keeping RPO within the 5-minute budget for most checkout sessions.
  - Alternatively, migrate checkout session storage from ElastiCache to DynamoDB Global Tables, which provides automatic multi-region replication. Update the checkout task definitions in both regions to use DynamoDB instead of Redis for session persistence.
- **Observability:**
  - If Global Datastore is adopted, add CloudWatch alarms on ElastiCache ReplicationLag metric for both replication groups, alerting when lag exceeds 240000ms (4 minutes) over 2 evaluation periods of 60 seconds. Publish a custom metric tracking checkout session creation rate per region to quantify data-at-risk during failover.
- **Testing:**
  - Execute the ARC Region Switch plan (mr-rs-plan-ngrh) to deactivate one region while active checkout sessions exist. Verify whether users can resume checkout in the surviving region. Measure the number of sessions lost to validate against the 5-minute RPO target.

### 2. ALB deletion protection disabled risks accidental infrastructure removal
- **Severity:** LOW
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2) have deletion_protection.enabled set to false. Accidental deletion of either load balancer would disrupt the entire application including the checkout flow, as the ALB fronts the UI service which is the entry point for all user traffic.
- **Mitigations:**
  - Enable deletion_protection on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2) by setting the deletion_protection.enabled attribute to true.
- **Observability:**
  - Deploy AWS Config rule elb-deletion-protection-enabled targeting both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2). Configure automatic remediation or SNS notification on NON_COMPLIANT evaluation to alert immediately if deletion protection is disabled.
- **Testing:**
  - Verify deletion protection by attempting to delete the ALB via CLI or console and confirming the operation is rejected. Include this check in infrastructure compliance validation.

### 3. FARGATE_SPOT exclusive capacity risks task interruptions during demand spikes
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The checkout ECS services in both regions use FARGATE_SPOT as the sole capacity provider with Weight=1 and Base=0. Fargate Spot tasks can be interrupted with a 2-minute warning when AWS reclaims capacity. During high-demand periods or Spot capacity shortages, tasks may be terminated or fail to launch, reducing available checkout capacity.
- **Mitigations:**
  - Add a mixed capacity provider strategy to the checkout-ngrh ECS services in both regions. Set FARGATE as the Base capacity provider (Base=2) to guarantee minimum always-on capacity, and use FARGATE_SPOT with Weight=1 for scaling beyond the baseline. This ensures core capacity is never interrupted while still benefiting from Spot cost savings for burst traffic.
- **Observability:**
  - Add CloudWatch alarms on ECS Service checkout-ngrh in both us-east-1 and us-west-2: alert when RunningTaskCount < DesiredCount for 2+ consecutive 1-minute periods. Use a math expression alarm comparing these metrics to detect Spot interruptions or capacity provisioning failures.
- **Testing:**
  - Simulate Spot interruption by stopping checkout tasks manually and measuring how quickly ECS replaces them. Verify that the service maintains acceptable response times with reduced task count during the replacement window.

### 4. ElastiCache Redis lacks replicas and automatic failover in both regions
- **Severity:** HIGH
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ElastiCache Redis replication groups (apche2zq6d0u0yh in us-east-1 and apctm8wm9gg5e7j in us-west-2) have AutomaticFailover disabled, MultiAZ disabled, and a single node with no replicas. If the primary node fails, checkout session state is lost and the service is unavailable until the node is replaced or regional failover completes.
- **Mitigations:**
  - Add at least one read replica to each ElastiCache replication group (apche2zq6d0u0yh in us-east-1 and apctm8wm9gg5e7j in us-west-2) in a different AZ from the primary. Enable AutomaticFailoverEnabled and MultiAZEnabled on both replication groups. Consider upgrading from cache.t3.micro to a larger instance type to support production workloads.
- **Observability:**
  - Add CloudWatch alarms for ElastiCache replication groups apche2zq6d0u0yh (us-east-1) and apctm8wm9gg5e7j (us-west-2): FreeableMemory < 50MB, EngineCPUUtilization > 80%, CurrConnections dropping to 0, and DatabaseMemoryUsagePercentage > 85%. Use 1-minute periods with 2 evaluation periods to detect single-node failures quickly.
- **Testing:**
  - After adding replicas, perform a failover test using the ElastiCache TestFailover API on each replication group. Verify that the checkout service reconnects to the new primary within acceptable latency bounds and that session data is preserved during the promotion.

### 5. ARC Region Switch sequential step timeouts may exceed 10-minute RTO
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** The ARC Region Switch plan executes ECS scaling (15-minute timeout), Aurora Global Database failover (20-minute timeout), and then DNS shift (5-minute timeout) sequentially. The DNS shift that actually restores service only executes after both prior steps complete. The combined worst-case execution time far exceeds the 10-minute RTO.
- **Mitigations:**
  - Reorder the ARC Region Switch plan to execute the Route 53 health check DNS shift step in parallel with or before the ECS scaling and database failover steps. Since checkout uses Redis (not Aurora), shifting traffic first and allowing scaling to complete afterward reduces time-to-recovery for checkout.
  - Pre-provision higher baseline capacity (increase MinCapacity from 2 to a level that can absorb cross-region traffic) so that scaling during failover is minimal or unnecessary, eliminating the scaling step from the critical path. Reduce the ECS scaling timeout from 15 minutes to a value closer to the RTO budget.
- **Observability:**
  - Add a CloudWatch alarm tracking ARC Region Switch plan execution duration for plan mr-rs-plan-ngrh, alerting if total execution exceeds 8 minutes (480 seconds). Add alarms on ECS checkout-ngrh service DesiredCount vs RunningTaskCount convergence time in both regions to identify FARGATE_SPOT capacity acquisition bottlenecks during failover.
- **Testing:**
  - Execute the ARC Region Switch plan (mr-rs-plan-ngrh) during peak traffic hours and measure wall-clock time from plan initiation to DNS shift completion. Validate that the checkout service in the surviving region is serving traffic within 10 minutes. Repeat with both us-east-1 and us-west-2 as the deactivated region.

### 6. No backup snapshots configured for checkout session state in Redis
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** MULTI_REGION_DISASTER_RECOVERY
- **Description:** Both ElastiCache Redis replication groups (apche2zq6d0u0yh in us-east-1 and apctm8wm9gg5e7j in us-west-2) have SnapshotRetentionLimit set to 0, disabling all automatic backup snapshots. No AWS Backup plan exists for these resources. A Redis failure, accidental FLUSHALL, or data corruption would result in permanent loss of all active checkout sessions with no recovery capability.
- **Mitigations:**
  - Enable automatic snapshots on both ElastiCache replication groups by setting SnapshotRetentionLimit to at least 7 days. Configure the SnapshotWindow during low-traffic periods to minimize performance impact.
  - Create an AWS Backup plan that includes both ElastiCache replication groups with backup frequency aligned to the 5-minute RPO where feasible. Store backup copies in a separate AWS Backup vault with vault lock enabled for ransomware protection.
  - Enable ElastiCache export-to-S3 for snapshots and configure S3 versioning on the destination bucket for additional protection against snapshot deletion.
- **Observability:**
  - Add CloudWatch alarms on AWS Backup BackupJobsFailed > 0 (Sum, 1-minute period) for ElastiCache backup jobs. Monitor ElastiCache SaveInProgress metric and alert if it remains at 1 for more than 10 minutes (indicating stalled snapshots). Add alarm on CurrItems dropping more than 50% compared to previous period to detect unexpected data loss.
- **Testing:**
  - Perform a quarterly restore test by restoring one of the ElastiCache replication groups from a snapshot into a test environment. Validate that the restored cluster contains expected session data and that the checkout service can connect and read state correctly. Measure restore duration against the 10-minute RTO target.


## orders (7 findings)

### 1. RabbitMQ brokers deployed as SINGLE_INSTANCE lack within-region high availability
- **Severity:** MEDIUM
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both RabbitMQ brokers (us-east-1 and us-west-2) are deployed in SINGLE_INSTANCE mode with a single subnet. A broker instance failure or AZ impairment causes OrderCreatedEvent publishing to fail in that region until the broker is recovered or replaced, which can exceed 10 minutes.
- **Mitigations:**
  - Upgrade both RabbitMQ brokers (retail-store-ar-ordersmq-ngrh in us-east-1 and us-west-2) from SINGLE_INSTANCE to CLUSTER_MULTI_AZ deployment mode. This provides automatic failover across AZs within each Region, ensuring OrderCreatedEvent publishing remains available during single-AZ failures without requiring a full Region switch.
  - If CLUSTER_MULTI_AZ cost is prohibitive, implement a circuit breaker in the orders service that gracefully degrades MQ publishing on broker failure, queuing events locally or to a durable SQS queue in the same Region for later replay when the broker recovers.
- **Observability:**
  - Monitor the AmazonMQ BrokerAvailability and BrokerUptime metrics for both brokers. Create CloudWatch alarms that trigger when BrokerAvailability drops below 1 for more than 1 minute. Also monitor RabbitMQMemUsed, SystemCpuUtilization, and MessageCount to detect broker health degradation before complete failure.
- **Testing:**
  - Perform a broker failover test by stopping the single-instance broker in one Region and verifying that order creation requests still succeed (order persisted to DSQL) while event publishing gracefully degrades. Measure time to broker recovery. Validate that no orders are lost in DSQL during the MQ outage.

### 2. No backup plan protects Aurora DSQL against logical corruption or accidental deletion
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The Aurora DSQL multi-region cluster (qft2zefei7duk5l7amhngnmwjy in us-east-1, vjt2zee6ahb655scmpcc2xunha in us-west-2) has no AWS Backup plan. Synchronous replication means logical corruption or accidental bulk deletion replicates instantly to both regions, leaving no recovery path.
- **Mitigations:**
  - Create an AWS Backup plan that includes the Aurora DSQL clusters (qft2zefei7duk5l7amhngnmwjy and vjt2zee6ahb655scmpcc2xunha). Configure daily snapshot backups with a retention period of at least 35 days. Store backup copies in a separate AWS account or use AWS Backup Vault Lock to create immutable backups that cannot be deleted even by administrators.
  - Enable AWS Backup's cross-account copy feature to store DSQL backups in an isolated backup account, providing an air-gapped recovery point that is resilient to account-level compromise or ransomware affecting the production account.
- **Observability:**
  - Monitor the AWS Backup job status via CloudWatch metrics aws/backup BackupJobsCompleted and BackupJobsFailed. Create an alarm that triggers when BackupJobsFailed > 0 for the backup vault containing DSQL resources, alerting the operations team via the critical-alarms SNS topics in both regions.
- **Testing:**
  - Perform a quarterly restore test by restoring the Aurora DSQL backup to a separate test cluster, then validating data integrity by comparing row counts and checksums of the orders table against the live cluster. Document the restore duration to confirm it fits within acceptable recovery timelines.

### 3. ALB target groups lack fail-open routing for coordinated health check failures
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALB target groups have minimum_healthy_targets.count set to 1 and percentage set to off. With only 2 targets per target group, a transient coordinated health check failure (e.g., dependency issue affecting /actuator) could leave no healthy targets, causing 503 errors to all clients.
- **Mitigations:**
  - Set target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage to 50 on both target groups (apps-n-AlbTa-N5B8VPEB42KO in us-east-1 and apps-n-AlbTa-YAIU6ZYVARN6 in us-west-2). This causes the ALB to route traffic to all targets when more than half fail health checks, preventing congestive collapse from coordinated transient failures.
- **Observability:**
  - Create CloudWatch alarms on HealthyHostCount dropping below 2 and UnHealthyHostCount exceeding 0 for both target groups. Alert on HTTPCode_ELB_5XX_Count spikes which indicate no healthy targets available.
- **Testing:**
  - Simulate coordinated health check failure by temporarily blocking the /actuator endpoint on all targets and verify the ALB fails open (continues routing traffic) rather than returning 503 to all clients.

### 4. Aurora DSQL clusters and production ALBs lack deletion protection
- **Severity:** MEDIUM
- **Category:** MISCONFIGURATION_AND_BUGS
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both Aurora DSQL clusters (qft2zefei7duk5l7amhngnmwjy, vjt2zee6ahb655scmpcc2xunha) and both production ALBs (apps-ngr-Alb-c29ylkrIPFgn, apps-ngr-Alb-AHlnhinNoTnn) have deletion protection disabled. Accidental deletion via API, CLI, or IaC misconfiguration could destroy the primary data store or disrupt all traffic routing.
- **Mitigations:**
  - Enable DeletionProtectionEnabled on both DSQL clusters (qft2zefei7duk5l7amhngnmwjy in us-east-1 and vjt2zee6ahb655scmpcc2xunha in us-west-2). This prevents accidental deletion of the orders database clusters.
  - Enable deletion_protection on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2) by setting the deletion_protection.enabled attribute to true.
- **Observability:**
  - Create an AWS Config rule to continuously evaluate that DeletionProtectionEnabled is true on both DSQL clusters and deletion_protection.enabled is true on both ALBs. Configure the rule to publish non-compliant findings to the critical-alarms SNS topics, alerting when deletion protection is inadvertently disabled.
  - Use CloudTrail event rules to detect any API calls attempting to modify or delete DSQL clusters or ALBs. Alert immediately on DeleteCluster, UpdateCluster, or DeleteLoadBalancer API calls targeting these resources.
- **Testing:**
  - Validate deletion protection by attempting delete API calls against non-production equivalents with deletion protection enabled, confirming the calls are rejected. Periodically audit the production resources' deletion protection attributes as part of infrastructure compliance checks.

### 5. ALB default keep-alive of 3600 seconds delays recovery from impaired nodes
- **Severity:** LOW
- **Category:** EXCESSIVE_LATENCY
- **Policy Component:** AVAILABILITY_SLO
- **Description:** Both ALBs have client_keep_alive.seconds set to the default 3600 (1 hour). Clients maintain persistent connections to ALB nodes for up to an hour, meaning if an ALB node becomes impaired, clients continue sending requests to it for up to 60 minutes before re-establishing connections.
- **Mitigations:**
  - Reduce client_keep_alive.seconds from 3600 to 180 on both ALBs (apps-ngr-Alb-c29ylkrIPFgn in us-east-1 and apps-ngr-Alb-AHlnhinNoTnn in us-west-2). This ensures clients re-establish connections every 3 minutes, limiting exposure to impaired ALB nodes.
- **Observability:**
  - Monitor ALB ActiveConnectionCount and NewConnectionCount metrics. A sudden drop in new connections or sustained high active connections on specific ALB nodes may indicate client connection pinning to impaired infrastructure.
- **Testing:**
  - After reducing client_keep_alive, run a sustained load test and verify that connection re-establishment overhead does not materially increase latency or error rates. Monitor NewConnectionCount to confirm the expected increase in connection cycling.

### 6. ECS services use exclusively FARGATE_SPOT with no on-demand baseline
- **Severity:** HIGH
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** All ECS services (orders-ngrh, ui-ngrh, checkout-ngrh in both regions) use CapacityProviderStrategy with FARGATE_SPOT weight=1 and base=0, meaning 100% of tasks run on interruptible Spot capacity. Fargate Spot tasks can be reclaimed with 2-minute warning, potentially terminating all running tasks simultaneously in one or both regions.
- **Mitigations:**
  - Update the CapacityProviderStrategy for all ECS services (orders-ngrh, ui-ngrh, checkout-ngrh in both regions) to use a base of at least 2 on FARGATE (on-demand) with FARGATE_SPOT for additional scaling capacity. This ensures minimum task count always runs on non-interruptible capacity.
  - Alternatively, switch entirely to FARGATE on-demand for all services given the 99.99% SLO requirement. The cost increase is justified by eliminating Spot interruption risk for a production tier-1 service.
- **Observability:**
  - Create CloudWatch alarms on ECS service RunningTaskCount dropping below DesiredCount for more than 1 minute. Monitor the Fargate Spot interruption rate via ECS service events and CloudWatch Container Insights task stop reasons.
- **Testing:**
  - Use AWS FIS with the aws:ecs:stop-task action to simulate Spot interruptions by stopping tasks in the orders-ngrh service. Verify that replacement tasks launch successfully and that the service recovers within the RTO. Test in one Region at a time to validate that the surviving Region absorbs traffic correctly.

### 7. ALB targets in only 2 AZs risks 50% capacity loss during AZ impairment
- **Severity:** MEDIUM
- **Category:** EXCESSIVE_LOAD
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The ALB target groups in both regions have targets registered in only 2 AZs. ECS services are configured with 3 subnets across 3 AZs but only 2 tasks are running (DesiredCount=2), meaning one AZ has no capacity. Losing the AZ with a target removes 50% of frontend capacity.
- **Mitigations:**
  - Increase the DesiredCount of the ui-ngrh ECS services in both regions from 2 to at least 3, ensuring tasks are distributed across all 3 configured AZs. With AvailabilityZoneRebalancing enabled, ECS will spread tasks evenly so that losing one AZ removes only one-third of capacity.
  - Similarly increase DesiredCount for orders-ngrh ECS services in both regions to at least 3 tasks, since the orders service is the primary function and also runs with only 2 tasks across 3 AZ subnets.
- **Observability:**
  - Monitor the HealthyHostCount metric per AZ on target groups apps-n-AlbTa-YAIU6ZYVARN6 and apps-n-AlbTa-N5B8VPEB42KO. Alert when any AZ has zero healthy targets, indicating unbalanced distribution.
- **Testing:**
  - After scaling to 3 tasks, verify task distribution across all 3 AZs by inspecting running task AZ placement. Then use AWS FIS aws:ecs:stop-task action targeting tasks in a single AZ to confirm the remaining AZs absorb traffic without errors.


## assets (1 findings)

### 1. ECS services use FARGATE_SPOT exclusively risking task interruptions
- **Severity:** HIGH
- **Category:** SINGLE_POINT_OF_FAILURE
- **Policy Component:** AVAILABILITY_SLO
- **Description:** The assets ECS services in both regions use FARGATE_SPOT with Weight=1 and Base=0, meaning 100% of tasks run on Spot capacity. Spot tasks can be interrupted with 2 minutes notice when AWS reclaims capacity, potentially taking all tasks offline simultaneously during capacity shortages.
- **Mitigations:**
  - Change the CapacityProviderStrategy on assets-ngrh ECS services in both regions to use a Base of 2 with FARGATE (on-demand) and Weight of 1 for FARGATE_SPOT. This ensures at least 2 tasks always run on reliable on-demand capacity while allowing scale-out tasks to use cheaper Spot capacity.
- **Observability:**
  - ADD CloudWatch alarms on ECS/ContainerInsights metric RunningTaskCount for service assets-ngrh in both us-east-1 (cluster apps-ngrh-EcsCluster-Gajk5ANQWU57) and us-west-2 (cluster apps-ngrh-EcsCluster-DfCQjH7kK2Bh). Use math expression RunningTaskCount < DesiredTaskCount, threshold 1, period 60s, evaluationPeriods 1, statistic Average, to detect Spot interruptions reducing capacity.
- **Testing:**
  - Use AWS FIS action aws:ecs:stop-task to terminate assets-ngrh tasks and verify that ECS replaces them within acceptable time bounds. Measure time-to-recovery and confirm the service remains available during task replacement.

## Evaluation Criteria for Future Runs

### Improvement (findings decrease)
- Adding FARGATE on-demand base capacity → removes 6 FARGATE_SPOT findings
- Enabling deletion protection → removes 5 protection findings
- Moving to 3-AZ VPC → removes 3 AZ findings
- Configuring ALB idle timeout → removes 4 keep-alive findings
- Adding AWS Backup plans → removes 3 backup findings

### Regression (findings increase)
- New findings in previously-clean areas indicate architecture degradation
- Finding count > 35 without new services = regression
- Same finding appearing with higher severity language = possible LLM non-determinism (re-run to confirm)

### Expected Variation
- NGRH findings are **LLM-generated and non-deterministic**. Exact wording may vary across runs.
- Finding **counts** are more stable than finding **prose**.
- A ±2 variance in total count is normal between runs with identical architecture.
- Finding IDs regenerate every assessment — do not compare by ID.
- Recommendations are specific and actionable but may vary in phrasing across runs.
