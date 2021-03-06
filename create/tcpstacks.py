from troposphere import Ref, Sub, Base64, FindInMap, Template, Output, GetAtt, ImportValue, Export, ec2, Tags, route53
from troposphere.events import Rule, Target
from troposphere import rds
from troposphere.cloudformation import WaitCondition, WaitConditionHandle
from troposphere.sns import Subscription
from troposphere import cloudwatch
from troposphere.cloudwatch import MetricDimension
from troposphere.policies import CreationPolicy, ResourceSignal
import argparse
import boto3
import json
import sys
from functools import partial
from . import iam
from . import custom_funcs
import awacs
import awacs.logs as logs
import create.network
from create import export_ref, import_ref
from .utils import update_dict
from .route53 import create_record_set
from collections import OrderedDict



def create_disk_cloudwatch_alarm(template, instance_id, resource_name, email_topic_arn, fs_mounts):
        count = 1
        for mount_path in fs_mounts:
                disk_dimensions     = [MetricDimension( Name = "MountPath",  Value  = mount_path,),
                                       MetricDimension( Name = "InstanceId", Value  = instance_id,)]
                namespace           = "System/Linux"
                metric              = "DiskSpaceUtilization"
                create_cloudwatch_alarm(template, instance_id, resource_name, email_topic_arn, disk_dimensions, namespace, metric, str(count))
                count += 1

def create_memory_cloudwatch_alarm(template, instance_id, resource_name, email_topic_arn):
        mem_dimensions      = [MetricDimension( Name = "InstanceId", Value  = instance_id,)]
        namespace           = "EC2/Memory"
        metric              = "MemUsage"
        create_cloudwatch_alarm(template, instance_id, resource_name, email_topic_arn, mem_dimensions, namespace, metric)


def create_cloudwatch_alarm(template, instance_id, resource_name, email_topic_arn, dimensions, namespace, metric, count = ""):
    name                = "".join([resource_name,"Alarm",metric,count])
    alarm_description   = "Alarm for "+resource_name+" "+metric
    alarm_actions       = [email_topic_arn]
    ok_actions          = [email_topic_arn]
    threshold           = "70"
    period              = 300
    eval_periods        = 2
    comparison_operator = "GreaterThanThreshold"

    return template.add_resource(cloudwatch.Alarm(
        name,
        AlarmName = name,
        AlarmDescription = alarm_description,
        Dimensions   = dimensions,
        AlarmActions = alarm_actions,
        OKActions    = [],
        Threshold    = threshold,
        MetricName   = metric,
        Statistic    = "Maximum",
        Period       = period,
        Namespace    = namespace,
        EvaluationPeriods  = eval_periods,
        ComparisonOperator = comparison_operator,
    ))


def sub_stack_network(template, ops, app_cfn_options, stack_name, stack_setup):
    app_name = ops.app_name
    billing_id = ops.billing_id
    deploy_env = ops.deploy_env

    app_nets = [val for key,val in sorted(ops.app_networks.items())]
    nat_networks = ops.get("nat_hosts_sn")

    stack_ports         = stack_setup.get('ports', [])

    internal_ports      = stack_setup.get('internal_ports')
    if internal_ports:
        raise(NotImplementedError("Need setup for internal ports within a sub-stack. Currently all\
            ports within 'ports' settings are allowed within security group"))

    stack_type = stack_setup['stack_type']
    custom_stack_rules  = stack_setup.get('custom_rules')
    stack_networks      = stack_setup['networks']
    stack_sg_name       = app_cfn_options['network_names']['tcpstacks'][stack_name]['sg_name']
    nacl_name           = app_cfn_options['network_names']['tcpstacks'][stack_name]['nacl_name']


    use_nat = ops.get("use_nat")
    use_nat_gw = ops.get("use_nat_gw")

    if use_nat and use_nat_gw:
        raise(ValueError("Both Nat and Nat Gateway can not be turned on"))
    nat_id = None
    stack_subnets = dict()

    for count,(az,cidr) in enumerate(sorted(stack_networks.items())):
        if use_nat:
            nat_id = ops.nat_host_ids[az]
        if use_nat_gw:
            nat_id = ops.nat_gw_ids[az]
        net_name = app_cfn_options['network_names']['tcpstacks'][stack_name]['subnet_names'][count]
        subnet   = create.network.subnet(template, ops.vpc_id, net_name, cidr, ops.availability_zones[az], billing_id, deploy_env)
        stack_subnets[az] = subnet

        create.network.routetable(
            template,
            ops.vpc_id,
            "Route"+net_name,
            subnet,
            vpn_id = ops.get("ofc_vpn_id"),
            nat_id = nat_id,
            vpn_route = ops.get("vpn_route"),
            use_nat = ops.get("use_nat"),
            use_nat_gw = ops.get("use_nat_gw")
        )

    nacl = create.network.nacl(template, app_name+stack_name+"Nacl", ops.vpc_id)
    networks_cidrs = [v for k,v in stack_networks.items()]
    if nat_networks:
        networks_cidrs.extend(nat_networks)
    custom_networks = set([cr[0] for cr in sorted(stack_setup.get("custom_rules"))])
#   Need to discuss this with Jeremy. Calling acl_add_networks with custom_networks list, It will open all the inbound ports based on the ports mentioned in yaml.
    #     next_rule_number=create.network.acl_add_networks(template, app_name+stack_name+"NaclRules", nacl, networks_cidrs + ops.get("deploy_hosts", []) + list(custom_networks))
    next_rule_number = create.network.acl_add_networks(template, app_name+stack_name+"NaclRules", nacl, networks_cidrs + ops.get("deploy_hosts", []))
    next_rule_number = create.network.acl_add_networks(template, app_name+stack_name+"NaclRules", nacl, custom_networks, start_rule = next_rule_number + 10)
    port_list = ['6|80|OutRule', '6|443|OutRule', '6|1024|65535|InRule','17|1024|65535|InRule']
    create.network.acl_add_networks(template, app_name + stack_name + "NaclRules", nacl, ["0.0.0.0/0"],start_rule=next_rule_number, ports=port_list)

    for count,(az,subnet) in enumerate(sorted(stack_subnets.items())):
        assoc_name = app_name+stack_name+"AclAssoc"+str(count)
        create.network.assoc_nacl_subnet(template, assoc_name, nacl, subnet)
    export_ref(
        template,
        export_name = nacl_name,
        value = nacl,
        desc = "{app_name} {tcpstack} Nacl".format(app_name = app_name, tcpstack=stack_name)
    )
    stack_nets = [val for key,val in sorted(stack_networks.items())]

    stack_sg = create.network.sec_group(
        template,
        name         = stack_sg_name,
        in_networks  = networks_cidrs,
        in_ports     = stack_ports,
        out_ports    = stack_ports,
        ssh_hosts    = ops.get("deploy_hosts"),
        ssh_ports    = [3389, 5985, 22],
        custom_rules = custom_stack_rules,
        ops          = ops,
    )
    export_ref(
        template,
        export_name = stack_sg_name,
        value = stack_sg,
        desc = "{app_name} {tcpstack} Security Group".format(app_name = app_name, tcpstack=stack_name)
    )

    stack_network_info = dict(
        stack_subnets = stack_subnets,
        stack_sg = stack_sg,
        stack_sg_name = stack_sg_name,
    )

    return stack_network_info

def linux_instance(template, instance_setup):
    resource_name   = instance_setup['resource_name']
    deploy_env      = instance_setup['deploy_env']
    billing_id      = instance_setup['billing_id']
    email_topic_arn = instance_setup['email_topic_arn']
    ami_image       = instance_setup['ami_image']
    subnet          = instance_setup['subnet']
    sg_name         = instance_setup['sg_name']
    keyname         = instance_setup.get("KeyName")
    fs_mounts       = instance_setup['fs_mounts']
    iam_profile     = instance_setup['iam_profile']
    private_ip_address  = instance_setup.get('ip_address')

    instance_size = instance_setup.get('instance_size')
    if not instance_size:
        instance_size = "t2.medium"

    userdata_file = instance_setup['userdata_file']

    userdata_1 = create.ec2.multipart_userdata(
        bash_files       = userdata_file,
        install_packages = ["docker"],
        sub_values       = instance_setup['userdata_vars'],
        env_vars         = instance_setup.get('environment')
    )

    if instance_setup['root_volume_size']:
            root_volume_size = instance_setup['root_volume_size']
    else:
            root_volume_size = "50"

    ebs_volume = ec2.EBSBlockDevice( VolumeSize = root_volume_size, VolumeType = "gp2", DeleteOnTermination = False)
    bdm = ec2.BlockDeviceMapping( DeviceName = '/dev/xvda', Ebs = ebs_volume)
    volumes = [ bdm ]

    if 'ebs_volume_size' in instance_setup:
        print('I am in EBS Setup')
        ebs_volume_size = instance_setup['ebs_volume_size']
        ebs_vol = ec2.EBSBlockDevice( VolumeSize = ebs_volume_size, VolumeType = "gp2", DeleteOnTermination = False)
        ebsbdm = ec2.BlockDeviceMapping( DeviceName = '/dev/xvdf', Ebs = ebs_vol)
        volumes.append(ebsbdm)


    ec2_args = dict(
        ImageId          = ami_image,
        InstanceType     = instance_size,
        SubnetId         = subnet,
        IamInstanceProfile = iam_profile,
        KeyName          = keyname,
        Tags             = Tags(
             Name = resource_name,
             Env = deploy_env,
             BillingID = billing_id
        ),
        SecurityGroupIds = [GetAtt(sg_name,"GroupId")],
        BlockDeviceMappings = volumes,
        UserData         = userdata_1,
        CreationPolicy   = CreationPolicy(
            ResourceSignal = ResourceSignal(Timeout = "PT100M")
        )
    )

    ec2_instance_func = partial(ec2.Instance, resource_name, **ec2_args)
    if instance_setup.get('build_serial') and instance_setup['previous_instance']:
        previous_instance = instance_setup['previous_instance']
        stack_instance = template.add_resource(ec2_instance_func(DependsOn = previous_instance))
    else:
        stack_instance = template.add_resource(ec2_instance_func())

    return stack_instance
    #if email_topic_arn:
        #create_disk_cloudwatch_alarm(template,Ref(stack_instance),resource_name,email_topic_arn, fs_mounts)
        #create_memory_cloudwatch_alarm(template,Ref(stack_instance),resource_name,email_topic_arn)

def windows_instance(template, instance_setup):
    resource_name = instance_setup['resource_name']
    deploy_env    = instance_setup['deploy_env']
    billing_id    = instance_setup['billing_id']
    ami_image     = instance_setup['ami_image']
    userdata_vars = instance_setup['userdata_vars']
    subnet        = instance_setup['subnet']
    sg_name       = instance_setup['sg_name']
    keyname       = instance_setup.get("KeyName")
    iam_profile   = instance_setup['iam_profile']
    private_ip    = instance_setup.get("private_ip")

    userdata_files = instance_setup['userdata_file']

    instance_size = instance_setup.get('instance_size')
    if not instance_size:
        instance_size = "t2.medium"


    ec2_args = dict(
        ImageId          = ami_image,
        InstanceType     = instance_size,
        SubnetId         = subnet,
        KeyName          = keyname,
        IamInstanceProfile = iam_profile,
        Tags             = Tags(
             Name = resource_name,
             Env = deploy_env,
             BillingID = billing_id
        ),
        SecurityGroupIds = [GetAtt(sg_name,"GroupId")],
        #BlockDeviceMappings = [bdm],
        CreationPolicy   = CreationPolicy(
            ResourceSignal = ResourceSignal(Timeout = "PT100M")
        )
    )
    if private_ip:
        ec2_args['PrivateIpAddress'] = private_ip

    if  instance_setup.get('userdata_file'):
        userdata = create.ec2.windows_cloudinit(
            powershell_files = userdata_files,
            sub_values = instance_setup['userdata_vars'],
        )
        ec2_args.update(
            Metadata         = userdata,
            UserData         = Base64(Sub("<script>\ncfn-init.exe -v --region ${AWS::Region} -r " + resource_name + " -s ${AWS::StackName} -c config1\n</script>")),
        )

    ec2_instance_func = partial(ec2.Instance, resource_name, **ec2_args)
    if instance_setup.get('build_serial') and instance_setup['previous_instance']:
        previous_instance = instance_setup['previous_instance']
        stack_instance = template.add_resource(ec2_instance_func(DependsOn = previous_instance))
    else:
        stack_instance = template.add_resource(ec2_instance_func())

def parent_yaml_fallback(ops, stack_setup, instance_setup, option_key):
    if instance_setup.get(option_key):
        return instance_setup[option_key]
    if stack_setup.get(option_key):
        return stack_setup[option_key]
    if ops.get(option_key):
        return ops[option_key]
    raise(ValueError("{} required for tcp_stacks".format(option_key)))

def create_ec2_stack(template, ops, app_cfn_options, stack_name, stack_setup):

    app_name = ops.app_name


    stack_setup = stack_setup.copy()
    stack_setup['billing_id'] = ops.billing_id
    stack_setup['deploy_env'] = ops.deploy_env
    stack_setup['email_topic_arn'] = ops.get("email_topic_arn", None)

    stack_network_info  = sub_stack_network(template, ops, app_cfn_options, stack_name, stack_setup)
    stack_resource_name = "".join([ops.app_name, stack_name])

    fs_mounts = stack_setup.get('fs_mounts', [])

    stack_setup['iam_profile'] = ImportValue(app_cfn_options.resource_names['ec2_iam_profile'])
    userdata_vars = {k:ops.get(v) for k,v in ops.userdata_exports.items()}
    if ops.get("userdata_values"):
        userdata_vars.update(ops.get("userdata_values"))
    cf_param_refs = {k:v for k,v in app_cfn_options.cf_params.items()}
    if stack_setup.get("environment"):
        update_dict(userdata_vars, stack_setup.get("environment"))
    if stack_setup.get("prerun"):
        prerun_mappings = [(prerun_name, prerun_setup["var_export"]) for prerun_name,prerun_setup in stack_setup['prerun'].items()]
        for prerun_name, var_export in prerun_mappings:
            mapping_name = prerun_name.replace("_","-")
            update_dict(userdata_vars, {var_export:FindInMap("PrerunValues", mapping_name, "ReturnString")})
    userdata_vars.update(cf_param_refs)
    userdata_vars['LOG_GROUP'] = app_cfn_options.log_group

    if stack_setup["stack_type"] == "ec2":
        instance_create = linux_instance
    elif stack_setup["stack_type"] == "ec2_windows":
        instance_create = windows_instance
    else:
        raise(ValueError("Error, unknown ec2 stack type:"+stack_setup["stack_type"]))

    domains = []
    previous_instance = None

    for instance, instance_setup in stack_setup['instances'].items():
        az = instance_setup['az']
        userdata_vars_copy = userdata_vars.copy()
        update_dict(userdata_vars_copy, instance_setup.get('environment'))
        resource_name = "".join([stack_resource_name, instance, az])

        userdata_vars_copy['resource_name'] = resource_name
        if app_cfn_options.cf_params.get('KeyName'):
            instance_setup['KeyName'] = app_cfn_options.cf_params.get('KeyName')


        instance_setup['resource_name']     = resource_name
        instance_setup['deploy_env']        = stack_setup['deploy_env']
        instance_setup['billing_id']        = stack_setup['billing_id']
        instance_setup['iam_profile']       = stack_setup['iam_profile']
        instance_setup['subnet']            = stack_network_info['stack_subnets'][az]
        instance_setup['userdata_vars']     = userdata_vars_copy
        instance_setup['email_topic_arn']   = ops.get('email_topic_arn')
        instance_setup['sg_name']           = stack_network_info['stack_sg_name']
        instance_setup['previous_instance'] = previous_instance
        instance_setup['fs_mounts']         = fs_mounts
        instance_setup['build_serial']      = stack_setup.get('build_serial')
        instance_setup['ami_image']         = parent_yaml_fallback(ops, stack_setup, instance_setup, 'ami_image')
        instance_setup['instance_size']     = parent_yaml_fallback(ops, stack_setup, instance_setup, 'instance_size')

        stack_setup_userdata = stack_setup.get('userdata_file', [])
        instance_setup_userdata = instance_setup.get('userdata_file', [])
        if type(stack_setup_userdata) is str:
            stack_setup_userdata = stack_setup_userdata.split()
        if type(instance_setup_userdata) is str:
            instance_setup_userdata = instance_setup_userdata.split()
        instance_setup['userdata_file']   = stack_setup_userdata + instance_setup_userdata
        if ops.get('root_volume_size'):
            instance_setup['root_volume_size'] = ops['root_volume_size']
        stack_instance = instance_create(template, instance_setup)


        if instance_setup.get("domain"):
            domain = instance_setup['domain'][:-1]
            route53_zone = "".join([domain.split(".",-1)[-1],"."])
            create_record_set(
                template,
                "".join([app_name, instance, "Domain"]),
                stack_instance,
                instance_setup['domain'],
                route53_zone,
            )
        previous_instance = resource_name
