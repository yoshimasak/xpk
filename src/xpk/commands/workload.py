"""
Copyright 2024 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from ..core.commands import (
    run_command_with_updates,
    run_commands,
)
from ..core.core import (
    CLUSTER_METADATA_CONFIGMAP,
    VERTEX_TENSORBOARD_FEATURE_FLAG,
    AcceleratorTypeToAcceleratorCharacteristics,
    add_zone_and_project,
    check_if_workload_can_schedule,
    check_if_workload_exists,
    create_accelerator_label,
    create_machine_label,
    create_vertex_experiment,
    get_cluster_configmap,
    get_cpu_affinity,
    get_gke_outlier_dashboard,
    get_gpu_rxdm_cmd,
    get_gpu_rxdm_image,
    get_gpu_scheduler,
    get_gpu_tcp_volume,
    get_gpu_volume,
    get_user_workload_container,
    get_volumes,
    parse_env_config,
    wait_for_job_completion,
    xpk_current_version,
    zone_to_region,
)
from ..core.kueue import LOCAL_QUEUE_NAME
from ..core.nap import (
    get_autoprovisioning_node_selector_args,
    is_autoprovisioning_enabled,
)
from ..core.pathways import (
    ensure_pathways_workload_prerequisites,
    get_pathways_proxy_args,
    get_pathways_rm_args,
    get_pathways_unified_query_link,
    get_pathways_worker_args,
    get_user_workload_for_pathways,
)
from ..core.system_characteristics import (
    AcceleratorType,
    get_system_characteristics,
)
from ..core.workload import get_workload_list
from ..utils.console import get_user_input, xpk_exit, xpk_print
from ..utils.file import write_tmp_file
from .cluster import set_cluster_command
from ..core.workload_decorators import tcpxo_decorator, rdma_decorator
from . import cluster_gcluster

workload_create_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  labels:
    kueue.x-k8s.io/queue-name: {local_queue_name}  # Name of the LocalQueue
    xpk.google.com/workload: {args.workload}
  annotations:
    alpha.jobset.sigs.k8s.io/exclusive-topology: cloud.google.com/gke-nodepool # 1:1 job replica to node pool assignment
spec:
  ttlSecondsAfterFinished: {args.ttl_seconds_after_finished}
  failurePolicy:
    maxRestarts: {args.max_restarts}
  replicatedJobs:
    - name: slice-job
      replicas: {args.num_slices}
      template:
        spec:
          parallelism: {system.vms_per_slice}    # Equal to the number of VMs per slice
          completions: {system.vms_per_slice}    # Same as the above.
          backoffLimit: 0   # When any pod fails, the job is failed
          template:
            metadata:
              labels:
                xpk.google.com/workload: {args.workload}
            spec:
              schedulerName: {args.scheduler}
              restartPolicy: Never
              {affinity}
              nodeSelector:
                {accelerator_label}
                {machine_label}
                {autoprovisioning_args}
              priorityClassName: {args.priority}
              hostNetwork: true
              dnsPolicy: ClusterFirstWithHostNet
              terminationGracePeriodSeconds: {args.termination_grace_period_seconds}
              containers:
              {container}
              volumes:
              {volumes}
"""


gpu_workload_create_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  labels:
    kueue.x-k8s.io/queue-name: multislice-queue  # Name of the LocalQueue
    xpk.google.com/workload: {args.workload}
spec:
  ttlSecondsAfterFinished: {args.ttl_seconds_after_finished}
  failurePolicy:
    maxRestarts: {args.max_restarts}
  replicatedJobs:
    - name: slice-job
      replicas: 1
      template:
        spec:
          parallelism: {args.num_nodes}
          completions: {args.num_nodes}
          backoffLimit: 0   # When any pod fails, the job is failed
          template:
            metadata:
              labels:
                xpk.google.com/workload: {args.workload}
            spec:
              {gpu_scheduler}
              priorityClassName: {args.priority}
              restartPolicy: Never
              hostNetwork: true
              dnsPolicy: ClusterFirstWithHostNet
              terminationGracePeriodSeconds: {args.termination_grace_period_seconds}
              tolerations:
              - operator: "Exists"
                key: nvidia.com/gpu
              volumes:
              {gpu_volume}
              containers:
              {gpu_rxdm_image}
                imagePullPolicy: Always
                command:
                - "bash"
                - "-c"
                - |
                  {gpu_rxdm_cmd} &
                  while [ ! -e "/usr/share/workload/workload_terminated" ]; do sleep 10; echo "sleeping"; done
                securityContext:
                  privileged: true
                volumeMounts:
                {gpu_tcp_volume}
                - name: nvidia-install-dir-host
                  mountPath: /usr/local/nvidia/lib64
                - name: workload-terminated-volume
                  mountPath: /usr/share/workload
                env:
                - name: LD_LIBRARY_PATH
                  value: /usr/local/nvidia/lib64
              {container}
"""

a3_gpu_workload_create_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  labels:
    kueue.x-k8s.io/queue-name: multislice-queue  # Name of the LocalQueue
    xpk.google.com/workload: {args.workload}
spec:
  ttlSecondsAfterFinished: {args.ttl_seconds_after_finished}
  failurePolicy:
    maxRestarts: {args.max_restarts}
  replicatedJobs:
    - name: slice-job
      replicas: 1
      template:
        spec:
          parallelism: {args.num_nodes}
          completions: {args.num_nodes}
          backoffLimit: 0   # When any pod fails, the job is failed
          template:
            metadata:
              labels:
                xpk.google.com/workload: {args.workload}
              annotations:
                kueue.x-k8s.io/podset-preferred-topology: "cloud.google.com/gce-topology-host"
            spec:
              priorityClassName: {args.priority}
              restartPolicy: Never
              dnsPolicy: ClusterFirstWithHostNet
              terminationGracePeriodSeconds: {args.termination_grace_period_seconds}
              tolerations:
              - operator: "Exists"
                key: nvidia.com/gpu
              containers:
              {container}
"""

pw_workload_create_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  labels:
    kueue.x-k8s.io/queue-name: {local_queue_name}  # Name of the LocalQueue
    xpk.google.com/workload: {args.workload}
spec:
  ttlSecondsAfterFinished: {args.ttl_seconds_after_finished}
  failurePolicy:
    maxRestarts: {args.max_restarts}
  successPolicy:
    operator: "All"
    targetReplicatedJobs:
    - {args.targetReplicatedJob}
  replicatedJobs:
  - name: worker
    replicas: {args.num_slices}
    template:
      metadata:
        annotations:
          alpha.jobset.sigs.k8s.io/exclusive-topology: cloud.google.com/gke-nodepool
        labels:
          xpk.google.com/workload: {args.workload}
      spec:
        backoffLimit: {backoff_limit}
        completions: {system.vms_per_slice}
        parallelism: {system.vms_per_slice}
        template:
          spec:
            terminationGracePeriodSeconds: {args.termination_grace_period_seconds}
            containers:
            - args:
              {pathways_worker_args}
              image: {args.server_image}
              imagePullPolicy: Always
              name: pathways-worker
              ports:
              - containerPort: 29001
              - containerPort: 8471
              - containerPort: 8080
              resources:
                limits:
                  {resource_type}: {system.chips_per_vm}
              securityContext:
                privileged: true
              volumeMounts:
              - mountPath: /tmp
                name: shared-tmp
            nodeSelector:
              {accelerator_label}
              {machine_label}
              {autoprovisioning_args}
            priorityClassName: {args.priority}
            hostNetwork: true
            dnsPolicy: ClusterFirstWithHostNet
            volumes:
            - hostPath:
                path: /tmp
                type: DirectoryOrCreate
              name: shared-tmp
  - name: rm
    replicas: 1
    template:
      metadata:
        labels:
          xpk.google.com/workload: {args.workload}
      spec:
        backoffLimit: 0
        completions: 1
        parallelism: 1
        template:
          spec:
            containers:
            - args:
              {pathways_rm_args}
              env:
              - name: REPLICATED_JOB_NAME
                valueFrom:
                  fieldRef:
                    fieldPath: metadata.annotations['jobset.sigs.k8s.io/replicatedjob-name']
              - name: JOBSET_NAME
                valueFrom:
                  fieldRef:
                    fieldPath: metadata.annotations['jobset.sigs.k8s.io/jobset-name']
              - name: HOST_ADDRESS
                value: $(JOBSET_NAME)-$(REPLICATED_JOB_NAME)-0-0.$(JOBSET_NAME)
              - name: TPU_SKIP_MDS_QUERY
                value: "true"
              image: {args.server_image}
              imagePullPolicy: Always
              name: pathways-rm
              ports:
              - containerPort: 29001
              securityContext:
                privileged: true
              volumeMounts:
              - mountPath: /tmp
                name: shared-tmp
            nodeSelector:
              cloud.google.com/gke-nodepool: cpu-rm-np
            hostNetwork: true
            dnsPolicy: ClusterFirstWithHostNet
            volumes:
            - hostPath:
                path: /tmp
                type: DirectoryOrCreate
              name: shared-tmp
  - name: proxy
    replicas: 1
    template:
      metadata:
        labels:
          xpk.google.com/workload: {args.workload}
      spec:
        backoffLimit: 0
        completions: 1
        parallelism: 1
        template:
          spec:
            containers:
            - args:
              {pathways_proxy_args}
              image: {args.proxy_server_image}
              imagePullPolicy: Always
              name: pathways-proxy
              ports:
              - containerPort: 29000
            hostNetwork: true
            dnsPolicy: ClusterFirstWithHostNet
            nodeSelector:
              cloud.google.com/gke-nodepool: cpu-proxy-np
  {user_workload}
"""


def workload_create_pathways(args) -> None:
  """Run jobset apply command for a file, specifically for Pathways.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  args.use_pathways = True
  if args.headless:
    xpk_print(
        'Please use kubectl port forwarding to connect to the Pathways proxy.'
        ' kubectl get pods kubectl port-forward <proxy-pod-name> 29000:29000'
        ' JAX_PLATFORMS=proxy JAX_BACKEND_TARGET=grpc://127.0.0.1:29000 python'
        " -c 'import pathwaysutils; import jax; print(jax.devices())'"
    )
  workload_create(args)


def workload_create(args) -> None:
  """Run jobset apply command for a file.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  add_zone_and_project(args)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  workload_exists = check_if_workload_exists(args)

  if workload_exists:
    xpk_print(
        f'{args.workload} already exists, XPK will not create this workload.'
        ' Please pick a new workload name'
    )
    xpk_exit(1)

  xpk_print('Starting workload create', flush=True)
  system, return_code = get_system_characteristics(args)

  if return_code > 0:
    xpk_print('Fetching system characteristics failed!')
    xpk_exit(return_code)

  if not check_if_workload_can_schedule(args, system):
    xpk_exit(1)

  xpk_print('Starting workload create', flush=True)

  metadata_configmap_name = f'{args.cluster}-{CLUSTER_METADATA_CONFIGMAP}'
  cluster_config_map = get_cluster_configmap(args, metadata_configmap_name)
  cluster_xpk_version = None
  if cluster_config_map is None:
    xpk_print(
        f'Warning: Unable to find ConfigMap: {metadata_configmap_name} for the'
        ' cluster. We recommend to upgrade your cluster by running `xpk'
        ' cluster create`.'
    )
  else:
    cluster_xpk_version = cluster_config_map.get('xpk_version')
  if (
      cluster_xpk_version is not None
      and cluster_xpk_version != xpk_current_version
  ):
    xpk_print(
        'Warning: Cluster has been created using XPK version:'
        f' {cluster_config_map["xpk_version"]} but the XPK version you are'
        f' using to schedule workload is: {xpk_current_version}. Some features'
        ' might not be available for this cluster. We recommend to'
        ' upgrade/downgrade your XPK version or cluster by running `xpk'
        ' cluster create`.'
    )

  debugging_dashboard_id = None

  tensorboard_config = {}
  if VERTEX_TENSORBOARD_FEATURE_FLAG and args.use_vertex_tensorboard:
    tensorboard_config = create_vertex_experiment(args)
    # exit if failed to create Experiment in Vertex AI
    if not tensorboard_config:
      xpk_exit(1)

  parse_env_config(args, tensorboard_config, system)

  # Currently autoprovisioning is not enabled for Pathways workloads.
  autoprovisioning_args = ''
  autoprovisioning_enabled, return_code = is_autoprovisioning_enabled(
      args, system
  )
  if return_code != 0:
    xpk_exit(return_code)
  if autoprovisioning_enabled:
    # Determine NAP capacity type
    autoprovisioning_args, return_code = (
        get_autoprovisioning_node_selector_args(args)
    )
    if return_code != 0:
      xpk_exit(return_code)

  # Create the workload file based on accelerator type or workload type.
  if system.accelerator_type == AcceleratorType['GPU']:
    container, debugging_dashboard_id = get_user_workload_container(
        args, system
    )
    gpu_scheduler, return_code = get_gpu_scheduler(
        args, system, autoprovisioning_args
    )
    if return_code != 0:
      xpk_exit(return_code)

    if system.device_type in cluster_gcluster.supported_device_types:
      yml_string = a3_gpu_workload_create_yaml.format(
          args=args, container=container
      )

      if args.device_type == cluster_gcluster.a3mega_device_type:
        sub_networks = [f'{args.cluster}-gpunet-{i}-subnet' for i in range(8)]
        yml_string = tcpxo_decorator.decorate_jobset(yml_string, sub_networks)

      if args.device_type == cluster_gcluster.a3ultra_device_type:
        sub_networks = [f'{args.cluster}-sub-1'] + [
            f'{args.cluster}-rdma-sub-{i}' for i in range(8)
        ]
        yml_string = rdma_decorator.decorate_jobset(yml_string, sub_networks)
    else:
      yml_string = gpu_workload_create_yaml.format(
          args=args,
          container=container,
          command=args.command,
          chips_per_vm=system.chips_per_vm,
          gpu_scheduler=gpu_scheduler,
          gpu_volume=get_gpu_volume(system),
          gpu_rxdm_image=get_gpu_rxdm_image(system),
          gpu_rxdm_cmd=get_gpu_rxdm_cmd(system),
          gpu_tcp_volume=get_gpu_tcp_volume(system),
      )
  elif args.use_pathways and ensure_pathways_workload_prerequisites(
      args, system
  ):
    yml_string = pw_workload_create_yaml.format(
        args=args,
        system=system,
        accelerator_label=create_accelerator_label(
            system.accelerator_type, system
        ),
        machine_label=create_machine_label(system.accelerator_type, system),
        pathways_rm_args=get_pathways_rm_args(args, system),
        pathways_worker_args=get_pathways_worker_args(args),
        pathways_proxy_args=get_pathways_proxy_args(args),
        user_workload=get_user_workload_for_pathways(args, system),
        resource_type=AcceleratorTypeToAcceleratorCharacteristics[
            system.accelerator_type
        ].resource_type,
        local_queue_name=LOCAL_QUEUE_NAME,
        autoprovisioning_args=autoprovisioning_args,
        backoff_limit=system.vms_per_slice * 4,
    )
  else:
    container, debugging_dashboard_id = get_user_workload_container(
        args, system
    )
    yml_string = workload_create_yaml.format(
        args=args,
        system=system,
        container=container,
        affinity=get_cpu_affinity(system.accelerator_type),
        accelerator_label=create_accelerator_label(
            system.accelerator_type, system
        ),
        machine_label=create_machine_label(system.accelerator_type, system),
        local_queue_name=LOCAL_QUEUE_NAME,
        autoprovisioning_args=autoprovisioning_args,
        volumes=get_volumes(args, system),
    )
  tmp = write_tmp_file(yml_string)
  command = f'kubectl apply -f {str(tmp.file.name)}'
  return_code = run_command_with_updates(command, 'Creating Workload', args)

  if return_code != 0:
    xpk_print(f'Create Workload request returned ERROR {return_code}')
    xpk_exit(return_code)

  # Get GKE outlier dashboard for TPU
  outlier_dashboard_id = None
  if system.accelerator_type == AcceleratorType['TPU']:
    outlier_dashboard_id = get_gke_outlier_dashboard(args)

  # Outlier and debugging dashboards
  if outlier_dashboard_id is not None:
    xpk_print(
        'Check statistics and outlier mode of GKE metrics here:'
        # pylint: disable=line-too-long
        f' https://console.cloud.google.com/monitoring/dashboards/builder/{outlier_dashboard_id}?project={args.project}&f.rlabel.cluster_name.ClusterName={args.cluster}.'
        ' To view the metric data for your workload, select'
        f' {args.workload} from the JobName filter on the dashboard.'
    )

  if debugging_dashboard_id is not None:
    xpk_print(
        'Check stack traces collected in Cloud Logging here:'
        # pylint: disable=line-too-long
        f' https://console.cloud.google.com/monitoring/dashboards/builder/{debugging_dashboard_id}?project={args.project}&f.rlabel.cluster_name.ClusterName={args.cluster}.'
        ' To view the stack traces for your workload, select'
        f' {args.workload} from the JobName filter on the dashboard.'
    )

  if args.use_pathways:
    if args.headless:
      xpk_print(
          ' \n ******* Please connect to your Pathways proxy at'
          f' {args.pathways_proxy_address}, once you see "IFRT proxy server'
          ' started with status OK" on the proxy link below.'
          ' Remember to delete the workload once done! ****** \n'
      )
      pathways_proxy_link = f'https://console.cloud.google.com/kubernetes/job/{zone_to_region(args.zone)}/{args.cluster}/default/{args.workload}-proxy-0/details?project={args.project}'
      xpk_print(
          'Follow the proxy here:'
          # pylint: disable=line-too-long)
          f' {pathways_proxy_link} '
      )
    xpk_print(
        'Follow your Pathways workload and other resources here : '
        f'{get_pathways_unified_query_link(args)}'
    )
  else:
    xpk_print(
        'Follow your workload here:'
        # pylint: disable=line-too-long
        f' https://console.cloud.google.com/kubernetes/service/{zone_to_region(args.zone)}/{args.cluster}/default/{args.workload}/details?project={args.project}'
    )
    duration_of_logs = 'P1D'  # Past 1 Day
    xpk_print(
        'Follow your worker 0, slice 0 logs here:'
        ' Adjust the pod name'
        ' ([prefix]-slice-job-[slice_number]-[worker_number])'
        ' after clicking the url if you want other worker logs.'
        # pylint: disable=line-too-long
        f' https://console.cloud.google.com/logs/query;query=resource.type%3D%22k8s_container%22%0Aresource.labels.project_id%3D%22{args.project}%22%0Aresource.labels.location%3D%22{zone_to_region(args.zone)}%22%0Aresource.labels.cluster_name%3D%22{args.cluster}%22%0Aresource.labels.namespace_name%3D%22default%22%0Aresource.labels.pod_name:%22{args.workload}-slice-job-0-0-%22%20severity%3E%3DDEFAULT;storageScope=project;duration={duration_of_logs}?e=13802955&mods=allow_workbench_image_override&project={args.project}'
    )

  xpk_exit(0)


def workload_delete(args) -> None:
  """Function around workload delete.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print('Starting Workload delete', flush=True)
  add_zone_and_project(args)
  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  will_delete = True
  if not args.workload:
    xpk_print('Get the name of the workloads in the cluster.')
    return_code, return_value = get_workload_list(args)

    if return_code != 0:
      xpk_print(f'List Job request returned ERROR {return_code}')
      xpk_exit(return_code)
    # Skip the header
    workloads = [x.split(' ')[0] for x in return_value.splitlines()][1:]
    if workloads and not args.force:
      will_delete = get_user_input(
          f'Planning to delete {len(workloads)} workloads in the cluster'
          f' {args.cluster} including {workloads}. \nDo you wish to delete: y'
          ' (yes) / n (no):\n'
      )
  else:
    workloads = [args.workload]

  if not workloads:
    xpk_print(
        'There are no workloads to delete matching the filter in the cluster.'
    )
  elif not will_delete:
    xpk_print('Skipping delete command.')
  else:
    commands = []
    task_names = []
    for workload in workloads:
      args.workload = workload
      command = f'kubectl delete jobset {workload} -n default'
      task_name = f'WorkloadDelete-{workload}'
      commands.append(command)
      task_names.append(task_name)

    # Not batching deletion for single workload
    if len(workloads) == 1:
      return_code = run_command_with_updates(
          commands[0], 'Delete Workload', args
      )
    else:
      return_code = run_commands(
          commands, 'Delete Workload', task_names, batch=100
      )

    if return_code != 0:
      xpk_print(f'Delete Workload request returned ERROR {return_code}')
      xpk_exit(return_code)
  xpk_exit(0)


def workload_list(args) -> None:
  """Function around workload list.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print(args)

  xpk_print('Starting workload list', flush=True)
  add_zone_and_project(args)
  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  if args.wait_for_job_completion:
    return_code = wait_for_job_completion(args)
    if return_code != 0:
      xpk_print(f'Wait for job completion returned ERROR {return_code}')
      xpk_exit(return_code)
    args.filter_by_job = args.wait_for_job_completion

  return_code, return_value = get_workload_list(args)

  if return_code != 0:
    xpk_print(f'List Job request returned ERROR {return_code}')
    xpk_exit(return_code)
  xpk_print(f'Workload List Output:\n{return_value}')
  xpk_exit(0)
