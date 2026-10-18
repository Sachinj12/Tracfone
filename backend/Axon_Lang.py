"""UI labels for the scenario automation (English and Spanish)."""
def get_keywords(language='EN'):
    lang = (language or 'EN').upper().replace('-', '_')
    common = {
        'about_phone': ['About phone', 'About device'],
        'android_ver': ['Android version'],
        'check_update': ['Check for update', 'Check for updates', 'Check now'],
        'clear_all': ['Clear all'],
        'device_details': ['Device details'],
        'hardware_info': ['Hardware information'],
        'device_ids': ['Device identifiers'],
        'dialpad': ['Keypad', 'Dialpad', 'Dial pad'],
        'model': ['Model'],
        'phone_0': ['Phone 0', 'Phone 0 (SIM 1)'],
        'phone_1': ['Phone 1', 'Phone 1 (SIM 2)'],
        'phone_app': ['Phone'],
        'phone_info': ['Phone information', 'Phone info'],
        'restart': ['Restart now', 'Please restart now'],
        'system': ['System'],
        'system_update': ['System updates', 'System update', 'Software updates', 'Software update'],
        'update_done': ['Update complete', 'System update complete', 'Software update complete', 'Your device is up to date'],
    }
    spanish = {
        'about_phone': ['Acerca del teléfono', 'Acerca del dispositivo', 'Información del teléfono'],
        'android_ver': ['Versión de Android'],
        'check_update': ['Buscar actualización', 'Buscar actualizaciones', 'Comprobar actualización', 'Buscar ahora'],
        'clear_all': ['Borrar todo', 'Eliminar todo'],
        'device_details': ['Detalles del dispositivo', 'Detalles del teléfono'],
        'hardware_info': ['Información de hardware', 'Información del hardware'],
        'device_ids': ['Identificadores del dispositivo', 'Identificadores de dispositivo'],
        'dialpad': ['Teclado', 'Teclado de marcación', 'Teclado numérico'],
        'model': ['Modelo'],
        'phone_0': ['Teléfono 0', 'Teléfono 0 (SIM 1)', 'Teléfono 0 (ranura SIM 1)'],
        'phone_1': ['Teléfono 1', 'Teléfono 1 (SIM 2)', 'Teléfono 1 (ranura SIM 2)'],
        'phone_app': ['Teléfono'],
        'phone_info': ['Información del teléfono', 'Información del teléfono', 'Información telefónica'],
        'restart': ['Reiniciar ahora', 'Reiniciar'],
        'system': ['Sistema'],
        'system_update': ['Actualizaciones del sistema', 'Actualización del sistema', 'Actualización de software', 'Actualizaciones de software'],
        'update_done': ['Actualización completada', 'Actualización del sistema completada', 'Actualización de software completada', 'Tu dispositivo está actualizado', 'El dispositivo está actualizado'],
    }
    if lang in ('ES', 'ES_ES', 'SPANISH'):
        # Spanish first, English alternatives last for devices with mixed-language UI.
        return {key: spanish.get(key, values) + [v for v in values if v not in spanish.get(key, [])]
                for key, values in common.items()}
    return common
