"""Pure validation of actual Docker states captured before normal cleanup."""
import math

SCHEMA='qiqc-native-final-containers/v1'


def validate_final_containers(value,startup_observation,security_evidence,verifier_exit):
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA:
        return ['terminal_evidence_missing_or_invalid']
    errors=[]
    startup=startup_observation.get('container_inspects',{})
    final=value.get('containers',{})
    for role in ('main','qsim','verifier'):
        current=final.get(role,{})
        original=startup.get(role,{})
        if not original.get('Id') or current.get('Id')!=original.get('Id'):
            errors.append(role+'_terminal_identity_mismatch')
        if current.get('Image')!=original.get('Image') or not current.get('Image'):
            errors.append(role+'_terminal_image_mismatch')
        if value.get('observed_before_cleanup',{}).get(role) is not True:
            errors.append(role+'_terminal_not_before_cleanup')
        timestamp=value.get('collected_at',{}).get(role)
        if type(timestamp) not in (int,float) or not math.isfinite(timestamp) or timestamp<=0:
            errors.append(role+'_terminal_time_missing')
        state=current.get('State',{})
        if state.get('OOMKilled') is not False:errors.append(role+'_terminal_oom_or_missing')
        if state.get('Dead') is not False or state.get('Restarting') is not False:
            errors.append(role+'_terminal_dead_restarting_or_missing')
        if current.get('RestartCount')!=0:errors.append(role+'_terminal_restarted_or_missing')
        if state.get('Error')!='':errors.append(role+'_terminal_engine_error_or_missing')
        if role=='main':
            if state.get('Running') is not False or state.get('Status')!='exited':
                errors.append('main_terminal_not_stopped')
            if security_evidence.get('main_stopped_before_verifier') is not True:
                errors.append('main_terminal_stop_not_planned')
            before=security_evidence.get('main_before_planned_stop',{})
            if before.get('Id')!=original.get('Id') or before.get('State',{}).get('Running') is not True:
                errors.append('main_exited_before_planned_stop')
        elif state.get('Running') is not True or state.get('Status')!='running':
            errors.append(role+'_terminal_unexpected_exit')
    if type(verifier_exit) is not int or verifier_exit not in (0,1):
        errors.append('terminal_verifier_process_not_completed')
    errors.extend(value.get('capture_errors',[]))
    return sorted(set(errors))
