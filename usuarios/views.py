from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.core.mail import send_mail
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db.models import Sum
from datetime import date

from .forms import RegistroForm, UsuarioForm
from .models import Rol, TipoDocumento
from ventas.models import Pedido
from produccion.models import Produccion
from inventario.models import Merma


# Usamos get_user_model() para obtener correctamente el modelo personalizado de Aviara
Usuario = get_user_model()


def landing(request):
    return render(request, 'landing.html')


@login_required
def home(request):
    user = request.user

    # 1. Administrador
    if user.is_staff or user.is_superuser or user.groups.filter(name__iexact='Administrador').exists():
        hoy = date.today()
        ventas_hoy = Pedido.objects.filter(fecha_pedido=hoy).aggregate(Sum('total_pedido'))['total_pedido__sum'] or 0
        produccion_hoy = Produccion.objects.filter(fecha_produccion=hoy).aggregate(Sum('cantidad_recolectada'))['cantidad_recolectada__sum'] or 0
        mermas_hoy = Merma.objects.filter(fecha_reporte=hoy).aggregate(Sum('cantidad_perdida'))['cantidad_perdida__sum'] or 0

        context = {
            'ventas_hoy': ventas_hoy,
            'produccion_hoy': produccion_hoy,
            'mermas_hoy': mermas_hoy,
        }
        return render(request, 'admin/home.html', context)

    # 2. Otros roles
    elif user.groups.filter(name__iexact='Operador').exists():
        return redirect('dashboard_operador')

    elif user.groups.filter(name__iexact='Distribuidor').exists():
        return redirect('dashboard_distribuidor')

    # 3. Cliente común
    else:
        return redirect('perfil_cliente')


@login_required
def perfil_cliente(request):
    """Página de perfil del cliente con historial de pedidos."""
    pedidos = Pedido.objects.filter(usuario=request.user).order_by('-fecha_pedido')
    return render(request, 'usuarios/perfil_cliente.html', {
        'pedidos': pedidos,
        'usuario': request.user,
    })


def registro(request):
    if request.method == 'POST':
        form = RegistroForm(request.POST)

        if form.is_valid():
            user = form.save()

            asunto = '¡Bienvenido a Aviara!'
            mensaje = f'Hola {user.username}, gracias por registrarte en nuestra página de Aviara.'
            email_desde = settings.EMAIL_HOST_USER
            email_para = [user.email]

            try:
                send_mail(asunto, mensaje, email_desde, email_para)
            except Exception as e:
                print(f"Error enviando correo: {e}")

            messages.success(request, "Cuenta creada exitosamente.")
            return redirect('login')
    else:
        form = RegistroForm()

    return render(request, 'registration/registro.html', {'form': form})


@login_required
def admin_dashboard(request):
    if not request.user.is_staff:
        return redirect('landing')

    return render(request, 'admin/home.html')


@login_required
def lista_usuarios(request):
    usuarios = Usuario.objects.all().order_by('-date_joined')
    return render(request, 'usuarios/lista.html', {'usuarios': usuarios})


@login_required
def carga_masiva_usuarios(request):
    if request.method != 'POST':
        return redirect('lista_usuarios')

    archivo = request.FILES.get('archivo_excel')

    if not archivo:
        messages.error(request, "Debes seleccionar un archivo Excel para realizar la carga masiva.")
        return redirect('lista_usuarios')

    if not archivo.name.lower().endswith('.xlsx'):
        messages.error(request, "La carga masiva de usuarios solo acepta archivos Excel (.xlsx).")
        return redirect('lista_usuarios')

    try:
        from tablib import Dataset

        dataset = Dataset()
        dataset.load(archivo.read(), format='xlsx')

        if not dataset.headers:
            messages.error(request, "El archivo no tiene encabezados.")
            return redirect('lista_usuarios')

        encabezados = [str(h).strip().lower() for h in dataset.headers]

        columnas_requeridas = ['username', 'documento', 'tipo_documento', 'rol']
        faltantes = [col for col in columnas_requeridas if col not in encabezados]

        if faltantes:
            messages.error(request, f"Faltan columnas obligatorias: {', '.join(faltantes)}.")
            return redirect('lista_usuarios')

        creados = 0
        actualizados = 0
        errores = []

        def obtener(row, *nombres, defecto=''):
            for nombre in nombres:
                valor = row.get(nombre)
                if valor is not None and str(valor).strip() != '':
                    return str(valor).strip()
            return defecto

        def limpiar_booleano(valor, defecto=True):
            valor = str(valor).strip().lower()

            if valor in ['true', '1', 'si', 'sí', 'activo', 'yes']:
                return True

            if valor in ['false', '0', 'no', 'inactivo']:
                return False

            return defecto

        for numero_fila, row_original in enumerate(dataset.dict, start=2):
            try:
                row = {
                    str(k).strip().lower(): v
                    for k, v in row_original.items()
                }

                # Ignorar filas completamente vacías del Excel
                if all(valor is None or str(valor).strip() == '' for valor in row.values()):
                    continue

                username = obtener(row, 'username')
                documento = obtener(row, 'documento')
                tipo_documento_nombre = obtener(row, 'tipo_documento')
                rol_nombre = obtener(row, 'rol')

                if not username:
                    errores.append(f"Fila {numero_fila}: el usuario está vacío.")
                    continue

                if not documento:
                    errores.append(f"Fila {numero_fila}: el documento está vacío.")
                    continue

                if not tipo_documento_nombre:
                    errores.append(f"Fila {numero_fila}: el tipo de documento está vacío.")
                    continue

                if not rol_nombre:
                    errores.append(f"Fila {numero_fila}: el rol está vacío.")
                    continue

                tipo_documento, _ = TipoDocumento.objects.get_or_create(
                    nombre_tipo=tipo_documento_nombre,
                    defaults={'estado': True}
                )

                rol, _ = Rol.objects.get_or_create(
                    nombre_rol=rol_nombre,
                    defaults={'estado': True}
                )

                first_name = obtener(row, 'first_name', 'nombre')
                last_name = obtener(row, 'last_name', 'apellido')
                email = obtener(row, 'email', 'correo')
                telefono = obtener(row, 'telefono')
                password = obtener(row, 'password', 'contraseña', defecto='Aviara12345')

                is_active = limpiar_booleano(
                    obtener(row, 'is_active', 'estado', defecto='activo'),
                    defecto=True
                )

                is_staff = limpiar_booleano(
                    obtener(row, 'is_staff', defecto='false'),
                    defecto=False
                )

                if 'admin' in rol_nombre.lower() or 'administrador' in rol_nombre.lower():
                    is_staff = True

                usuario_existente_por_username = Usuario.objects.filter(
                    username=username
                ).exclude(
                    documento=documento
                ).first()

                if usuario_existente_por_username:
                    errores.append(f"Fila {numero_fila}: el username '{username}' ya existe con otro documento.")
                    continue

                datos_usuario = {
                    'username': username,
                    'first_name': first_name,
                    'last_name': last_name,
                    'email': email,
                    'tipo_documento': tipo_documento,
                    'telefono': telefono,
                    'rol': rol,
                    'is_staff': is_staff,
                    'is_active': is_active,
                    'estado': 'Activo' if is_active else 'Inactivo',
                }

                if password:
                    datos_usuario['password'] = make_password(password)

                usuario, creado = Usuario.objects.update_or_create(
                    documento=documento,
                    defaults=datos_usuario
                )

                if creado:
                    creados += 1
                else:
                    actualizados += 1

            except Exception as e:
                errores.append(f"Fila {numero_fila}: {str(e)}")

        if creados or actualizados:
            messages.success(
                request,
                f"Carga masiva finalizada. Usuarios creados: {creados}. Usuarios actualizados: {actualizados}."
            )

        if errores:
            messages.warning(
                request,
                "Algunas filas no se cargaron correctamente: " + " | ".join(errores[:5])
            )

        if not creados and not actualizados and not errores:
            messages.warning(request, "No se encontraron usuarios válidos para cargar.")

    except Exception as e:
        messages.error(request, f"Error al procesar el archivo: {str(e)}")

    return redirect('lista_usuarios')


@login_required
def editar_usuario(request, pk):
    usuario = get_object_or_404(Usuario, pk=pk)

    if request.method == 'POST':
        form = UsuarioForm(request.POST, instance=usuario)

        if form.is_valid():
            form.save()
            messages.success(request, f'Perfil de {usuario.username} actualizado.')
            return redirect('lista_usuarios')
        else:
            print(form.errors)
    else:
        form = UsuarioForm(instance=usuario)

    return render(request, 'usuarios/editar_usuario.html', {
        'form': form,
        'usuario': usuario
    })


@login_required
def inhabilitar_usuario(request, pk):
    usuario = get_object_or_404(Usuario, pk=pk)

    if usuario.username == request.user.username:
        messages.error(request, "No puede inhabilitar su propia cuenta.")
        return redirect('lista_usuarios')

    usuario.is_active = False
    usuario.estado = 'Inactivo'
    usuario.save()

    messages.success(request, f"El usuario {usuario.username} ha sido inhabilitado.")
    return redirect('lista_usuarios')


@login_required
def crear_usuario(request):
    if request.method == 'POST':
        form = UsuarioForm(request.POST)

        if form.is_valid():
            rol_seleccionado = form.cleaned_data.get('rol')

            if rol_seleccionado and str(rol_seleccionado).lower() == 'administrador':
                total_admins = Usuario.objects.filter(
                    rol=rol_seleccionado,
                    is_active=True
                ).count()

                if total_admins >= 3:
                    messages.error(
                        request,
                        "⚠️ Registro rechazado: Aviara ya cuenta con el límite máximo permitido (3 administradores activos)."
                    )
                    return render(request, 'usuarios/crear_usuario.html', {'form': form})

            form.save()
            messages.success(request, "Usuario creado con éxito.")
            return redirect('lista_usuarios')
    else:
        form = UsuarioForm()

    return render(request, 'usuarios/crear_usuario.html', {'form': form})