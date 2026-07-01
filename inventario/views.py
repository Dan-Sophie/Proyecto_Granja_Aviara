from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction, models
from django.db.models import Sum, Count, F, ExpressionWrapper, DecimalField
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone
from datetime import timedelta
from weasyprint import HTML, CSS

# Importaciones de tu app local
from .models import Merma, Lote
from .forms import MermaForm, MermaOperadorForm

# Función auxiliar para comprobar si es Administrador/Staff
def es_administrador(user):
    return user.is_staff

# =====================================================================
# GESTIÓN DE MERMAS Y AJUSTES (HU-05)
# =====================================================================

@login_required
def lista_mermas(request):
    """Muestra el historial de mermas enrutando correctamente según el rol"""
    if request.user.is_staff:
        # El Administrador ve todas las mermas registradas para gestionar aprobaciones
        mermas = Merma.objects.all().order_by('-fecha_reporte')
        return render(request, 'mermas/merma_list.html', {'mermas': mermas})
    else:
        # El Operador ve únicamente los registros que él mismo ha reportado
        mermas = Merma.objects.filter(reportado_por=request.user).order_by('-fecha_reporte')
        return render(request, 'operador/mermas/lista_mermas.html', {'mermas': mermas})


@login_required
def crear_merma(request):
    if request.method == 'POST':
        form = MermaForm(request.POST) if request.user.is_staff else MermaOperadorForm(request.POST)
        
        if form.is_valid():
            merma = form.save(commit=False)
            merma.reportado_por = request.user
            if not request.user.is_staff:
                merma.estado = 'PENDIENTE'
            merma.save()
            messages.success(request, "El reporte de merma ha sido registrado correctamente.")
            return redirect('lista_mermas')
    else:
        form = MermaForm() if request.user.is_staff else MermaOperadorForm()
        
    # --- RENDERIZADO DIVIDIDO CORREGIDO ---
    if request.user.is_staff:
        return render(request, 'mermas/mermas_crear.html', {'form': form})
    else:
        # Aquí va el render para el operador
        return render(request, 'operador/mermas/form_merma.html', {'form': form})


@login_required
@user_passes_test(es_administrador, login_url='dashboard_inventario')
def aprobar_merma(request, pk):
    """Acción exclusiva del Administrador para autorizar el descuento de inventario"""
    merma = get_object_or_404(Merma, pk=pk)
    if merma.estado == 'PENDIENTE':
        try:
            with transaction.atomic():
                lote = merma.lote
                if lote.cantidad_actual >= merma.cantidad_perdida:
                    lote.cantidad_actual -= merma.cantidad_perdida
                    lote.save()
                    merma.estado = 'APROBADA'
                    merma.save()
                    messages.success(request, f"Merma #{merma.id} aprobada con éxito. Inventario actualizado.")
                else:
                    messages.error(request, f"No se puede aprobar. El lote no tiene stock suficiente ({lote.cantidad_actual} unds disponibles).")
        except Exception as e:
            messages.error(request, f"Error al procesar la aprobación: {str(e)}")
    return redirect('lista_mermas')


@login_required
@user_passes_test(es_administrador, login_url='dashboard_inventario')
def rechazar_merma(request, pk):
    """Acción exclusiva del Administrador para invalidar el ajuste (HU-05 Escenario 3)"""
    merma = get_object_or_404(Merma, pk=pk)
    if merma.estado == 'PENDIENTE':
        merma.estado = 'RECHAZADA'
        merma.save()
        messages.warning(request, f"El reporte de merma #{merma.id} ha sido rechazado.")
    return redirect('lista_mermas')


@login_required
def informar_alerta(request):
    """Endpoint JSON que retorna los lotes que están a punto de agotarse"""
    # Buscamos los lotes que tengan pocas unidades (menos de 10) y que no estén en 0
    lotes_criticos = Lote.objects.filter(cantidad_actual__gt=0, cantidad_actual__lte=10)
    
    alertas = [
        {
            "id": lote.id,
            "codigo": lote.codigo_lote,
            "cantidad": lote.cantidad_actual,
            "mensaje": f"El lote {lote.codigo_lote} está críticamente bajo de stock ({lote.cantidad_actual} unds)."
        }
        for lote in lotes_criticos
    ]
    
    return JsonResponse({"alertas": alertas})


# =====================================================================
# GESTIÓN DE INVENTARIO (Panel General y Reportes)
# =====================================================================

@login_required
def dashboard_inventario(request):
    lotes = Lote.objects.filter(esta_activo=True)
    filtro_calidad = request.GET.get('calidad')
    
    if filtro_calidad:
        lotes = lotes.filter(estado_calidad=filtro_calidad)
        
    lotes = lotes.order_by('cantidad_actual')
    total_stock_fisico = lotes.aggregate(Sum('cantidad_actual'))['cantidad_actual__sum'] or 0
    
    lotes_criticos_conteo = sum(1 for lote in lotes if hasattr(lote, 'stock_minimo') and lote.cantidad_actual < lote.stock_minimo)

    context = {
        'lotes': lotes,
        'total_stock_fisico': total_stock_fisico,
        'lotes_criticos_conteo': lotes_criticos_conteo,
        'filtro_actual': filtro_calidad,
    }
    return render(request, 'inventario/inventario_dashboard.html', context)


@login_required
def reporte_inventario_pdf(request):
    hoy = timezone.now()
    
    # --- 1. FILTRO TEMPORAL ---
    filtro = request.GET.get('filtro', 'mes')
    if filtro == 'semana':
        fecha_inicio = hoy - timedelta(days=7)
        texto_filtro = "Últimos 7 Días (Semanal)"
    elif filtro == 'trimestre':
        fecha_inicio = hoy - timedelta(days=90)
        texto_filtro = "Últimos 3 Meses (Trimestral)"
    elif filtro == 'ano':
        fecha_inicio = hoy - timedelta(days=365)
        texto_filtro = "Último Año (Anual)"
    else:
        fecha_inicio = hoy - timedelta(days=30)
        texto_filtro = "Últimos 30 Días (Mensual)"

    # =========================================================
    # 🌟 CONTROL DE EXISTENCIAS DIRECTO DESDE LOS LOTES
    # =========================================================
    lotes_activos = Lote.objects.filter(cantidad_actual__gt=0)
    
    total_stock_disponible = lotes_activos.aggregate(
        total_unds=Sum('cantidad_actual'),
        valor_comercial=Sum(ExpressionWrapper(F('cantidad_actual') * F('producto__precio'), output_field=DecimalField()))
    )

    # =========================================================
    # 🌟 AUDITORÍA TEMPORAL DE MERMAS
    # =========================================================
    mermas_filtradas = Merma.objects.filter(
        fecha_reporte__range=[fecha_inicio, hoy],
        estado='APROBADA'
    ).select_related('lote', 'reportado_por')

    metricas_mermas = mermas_filtradas.aggregate(
        unidades_perdidas=Sum('cantidad_perdida'),
        total_lotes_afectados=Count('lote', distinct=True),
        ventas_perdidas=Sum(ExpressionWrapper(F('cantidad_perdida') * F('lote__producto__precio'), output_field=DecimalField()))
    )

    # =========================================================
    # 🌟 AUDITORÍA DE OPERADORES (GROUP BY)
    # =========================================================
    rendimiento_operadores = (
        mermas_filtradas.values('reportado_por__username')
        .annotate(
            total_registros=Count('id'),
            unidades_reportadas=Sum('cantidad_perdida')
        )
        .order_by('-total_registros')
    )

    context = {
        'lotes_stock': lotes_activos.order_by('-id'),
        'stock_totales': total_stock_disponible,
        'mermas': mermas_filtradas.order_by('-fecha_reporte')[:12],
        'metricas_mermas': metricas_mermas,
        'operadores': rendimiento_operadores,
        'filtro_seleccionado': texto_filtro,
        'fecha_generacion': hoy,
    }

    html_string = render_to_string('inventario/reporte_pdf_template.html', context)
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'inline; filename="reporte_analitico_aviara.pdf"'
    
    HTML(string=html_string, base_url=request.build_absolute_uri()).write_pdf(response)
    return response