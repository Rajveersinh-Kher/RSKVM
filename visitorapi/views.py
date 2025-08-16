from django.shortcuts import render, redirect, get_object_or_404
from rest_framework import viewsets, permissions
from .models import HRUser, Visitor, VisitRequest, VisitorCard
from .serializers import (
    HRUserSerializer, 
    VisitorSerializer, 
    VisitRequestSerializer, 
    VisitorCardSerializer
)
from django.views.generic import TemplateView
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import timedelta
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.db import models
import openpyxl
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.template.loader import render_to_string
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.contrib.auth.tokens import default_token_generator
from django.conf import settings
from django.urls import reverse
from django.core.mail import send_mail
import logging
from django.views.decorators.http import require_POST
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
import random
import string
from rest_framework.parsers import MultiPartParser, FormParser
from django.utils.timezone import localtime

# Create your views here.

class HRUserViewSet(viewsets.ModelViewSet):
    queryset = HRUser.objects.all()
    serializer_class = HRUserSerializer
    permission_classes = [permissions.IsAuthenticated]

class VisitorViewSet(viewsets.ModelViewSet):
    queryset = Visitor.objects.all()
    serializer_class = VisitorSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = Visitor.objects.all()
        email = self.request.query_params.get('email', None)
        if email is not None:
            queryset = queryset.filter(email=email)
        return queryset

class VisitRequestViewSet(viewsets.ModelViewSet):
    queryset = VisitRequest.objects.all()
    serializer_class = VisitRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = VisitRequest.objects.all()
        status = self.request.query_params.get('status', None)
        date = self.request.query_params.get('date', None)
        
        if status is not None:
            queryset = queryset.filter(status=status)
        if date is not None:
            queryset = queryset.filter(visit_date=date)
        
        return queryset

class VisitorCardViewSet(viewsets.ModelViewSet):
    queryset = VisitorCard.objects.all()
    serializer_class = VisitorCardSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = VisitorCard.objects.all()
        status = self.request.query_params.get('status', None)
        if status is not None:
            queryset = queryset.filter(status=status)
        return queryset

class VisitorRegistrationView(TemplateView):
    template_name = "visitor_form.html"

@login_required(login_url='/registration-login/')
def visitor_registration(request):
    if not is_registration_user(request.user):
        logout(request)
        messages.error(request, 'You do not have permission to access this portal.')
        return redirect('registration-login')
    from django.db.models import Count, Max
    from .models import Visitor
    # Top 10 frequent visitors
    frequent_visitors = (
        Visitor.objects.annotate(
            num_visits=Count('visitrequest'),
            last_visit=Max('visitrequest__created_at')
        )
        .filter(num_visits__gt=0)
        .order_by('-num_visits', '-last_visit')[:10]
    )
    return render(request, 'visitor_form.html', {'frequent_visitors': frequent_visitors})

@api_view(['POST'])
@permission_classes([AllowAny])
def register_visitor(request):
    # Check if user is authenticated and is a registration user
    if not request.user.is_authenticated or not is_registration_user(request.user):
        return Response({'error': 'Authentication required'}, status=401)
    try:
        # Determine host based on 'Send to HR' checkbox
        send_to_hr = request.data.get('send_to_hr') in ['on', 'true', 'True', True]
        if send_to_hr:
            host = HRUser.objects.filter(user_type='HR').first()
        else:
            host = HRUser.objects.filter(user_type='HOS').first()
        if not host:
            return Response({'error': 'No host available to assign the visit.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        email = request.data.get('email')
        visitor_data = {
            'first_name': request.data.get('first_name'),
            'last_name': request.data.get('last_name'),
            'phone': request.data.get('phone'),
            'company': request.data.get('company'),
            'id_proof_type': request.data.get('id_proof_type'),
            'id_proof_number': request.data.get('id_proof_number'),
            'photo': request.FILES.get('photo'),
        }

        if email:
            # If email is provided, update or create based on email
            visitor, created = Visitor.objects.update_or_create(
                email=email,
                defaults=visitor_data
            )
        else:
            # If no email, check for existing visitor by name, phone, and company
            existing_visitor = Visitor.objects.filter(
                first_name=visitor_data['first_name'],
                last_name=visitor_data['last_name'],
                phone=visitor_data['phone'],
                company=visitor_data['company']
            ).first()
            if existing_visitor:
                # Do NOT update the existing visitor's details!
                visitor = existing_visitor
            else:
                visitor = Visitor.objects.create(**visitor_data, email=None)

        # Handle device permissions (checkboxes return 'on' if checked, None if unchecked)
        allow_mobile = request.data.get('allow_mobile') == 'on'
        allow_laptop = request.data.get('allow_laptop') == 'on'

        # Reference by Employee fields
        requested_by_employee = request.data.get('reference_by_employee') in ['on', 'true', 'True', True]
        reference_employee_name = request.data.get('reference_employee_name') if requested_by_employee else None
        reference_employee_department = request.data.get('reference_employee_department') if requested_by_employee else None
        reference_purpose = request.data.get('reference_purpose') if requested_by_employee else None

        # Set created_by only if the user is a Registration user
        created_by = None
        if request.user.is_authenticated and hasattr(request.user, 'user_type') and request.user.user_type == 'REGISTRATION':
            created_by = request.user

        # Parse end_time and valid_upto from form
        from datetime import datetime, time
        end_time_str = request.data.get('end_time')
        if end_time_str:
            end_time = datetime.strptime(end_time_str, '%H:%M').time()
        else:
            end_time = time(17, 30)  # Default 17:30
        valid_upto_str = request.data.get('valid_upto')
        if valid_upto_str:
            valid_upto = datetime.strptime(valid_upto_str, '%Y-%m-%d').date()
        else:
            valid_upto = None

        # Create VisitRequest
        VisitRequest.objects.create(
            visitor=visitor,
            host=host,
            purpose=request.data.get('purpose'),
            other_purpose=request.data.get('other_purpose'),
            visit_date=timezone.now().date(),
            start_time=timezone.now(),
            end_time=end_time,
            status='PENDING',
            allow_mobile=allow_mobile,
            allow_laptop=allow_laptop,
            requestedByEmployee=requested_by_employee,
            reference_employee_name=reference_employee_name,
            reference_employee_department=reference_employee_department,
            reference_purpose=reference_purpose,
            created_by=created_by,
            # Add valid_upto field if present in model
            valid_upto=valid_upto,
        )

        return Response({'message': 'Visitor registered successfully. Your request is pending approval.'}, status=status.HTTP_201_CREATED)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

def is_hr_user(user):
    return hasattr(user, 'user_type') and user.user_type == 'HR'

def is_registration_user(user):
    return hasattr(user, 'user_type') and user.user_type == 'REGISTRATION'

def is_hos_user(user):
    return hasattr(user, 'user_type') and user.user_type == 'HOS'

# Update login_view to handle only HR users

def login_view(request):
    if request.user.is_authenticated:
        if is_hr_user(request.user):
            return redirect('hr-dashboard')
        else:
            logout(request)
            messages.error(request, 'You do not have permission to access this portal.')
            return render(request, 'login.html')
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            user_obj = User.objects.get(username=username)
        except User.DoesNotExist:
            messages.error(request, 'Invalid username.')
            return render(request, 'login.html')
        if not user_obj.check_password(password):
            messages.error(request, 'Invalid password.')
            return render(request, 'login.html')
        user = authenticate(request, username=username, password=password)
        if user is not None and is_hr_user(user):
            login(request, user)
            return redirect('hr-dashboard')
        else:
            messages.error(request, 'You do not have permission to access this portal.')
            return render(request, 'login.html')
    return render(request, 'login.html')

# Registration user login view

def registration_user_login_view(request):
    if request.user.is_authenticated:
        if is_registration_user(request.user):
            return redirect('visitor-registration')
        else:
            logout(request)
            messages.error(request, 'You do not have permission to access this portal.')
            return render(request, 'registration_login.html')
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            user_obj = User.objects.get(username=username)
        except User.DoesNotExist:
            messages.error(request, 'Invalid username.')
            return render(request, 'registration_login.html')
        if not user_obj.check_password(password):
            messages.error(request, 'Invalid password.')
            return render(request, 'registration_login.html')
        user = authenticate(request, username=username, password=password)
        if user is not None and is_registration_user(user):
            login(request, user)
            return redirect('visitor-registration')
        else:
            messages.error(request, 'You do not have permission to access this portal.')
            return render(request, 'registration_login.html')
    return render(request, 'registration_login.html')

# HOS login view

@csrf_protect
def hos_login_view(request):
    if request.user.is_authenticated:
        if is_hos_user(request.user):
            return redirect('hos-dashboard')
        else:
            logout(request)
            messages.error(request, 'You do not have permission to access this portal.')
            return render(request, 'hos_login.html')
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            user_obj = User.objects.get(username=username)
        except User.DoesNotExist:
            messages.error(request, 'Invalid username.')
            return render(request, 'hos_login.html')
        if not user_obj.check_password(password):
            messages.error(request, 'Invalid password.')
            return render(request, 'hos_login.html')
        user = authenticate(request, username=username, password=password)
        if user is not None and is_hos_user(user):
            login(request, user)
            return redirect('hos-dashboard')
        else:
            messages.error(request, 'You do not have permission to access this portal.')
            return render(request, 'hos_login.html')
    return render(request, 'hos_login.html')

# Restrict dashboards

@login_required(login_url='/login/')
def hr_dashboard_view(request):
    if not is_hr_user(request.user):
        logout(request)
        messages.error(request, 'You do not have permission to access this portal.')
        return redirect('login')
    now = timezone.now()
    today = now.date()
    seven_days_ago = now - timedelta(days=7)
    pending_requests = VisitRequest.objects.filter(status='PENDING', host__user_type='HR', created_at__gte=seven_days_ago).order_by('-created_at')
    approved_requests = VisitRequest.objects.filter(status='APPROVED', host__user_type='HR', created_at__gte=seven_days_ago).order_by('-created_at')
    rejected_requests = VisitRequest.objects.filter(status='REJECTED', host__user_type='HR', created_at__gte=seven_days_ago).order_by('-created_at')
    # Frequent users: annotate with visit count and last visit date (last 7 days only)
    recent_visitors = Visitor.objects.filter(visitrequest__host__user_type='HR', visitrequest__created_at__gte=seven_days_ago)
    frequent_visitors = (
        recent_visitors.annotate(
            num_visits=Count('visitrequest', filter=Q(visitrequest__created_at__gte=seven_days_ago, visitrequest__host__user_type='HR')),
            last_visit=Max('visitrequest__created_at', filter=Q(visitrequest__created_at__gte=seven_days_ago, visitrequest__host__user_type='HR'))
        )
        .filter(num_visits__gt=0)
        .order_by('-num_visits', '-last_visit')[:10]
    )
    # Use 7-day filter for all_visits for the 3 buttons, but for checked-in visitors, use all visits
    all_visits = VisitRequest.objects.select_related('visitor').filter(host__user_type='HR', created_at__gte=seven_days_ago)
    all_visits_for_checked_in = VisitRequest.objects.select_related('visitor').filter(host__user_type='HR')

    # Checked-in visitors for any day (multi-day aware, no 7-day filter)
    checked_in_visitors = []
    for vr in all_visits_for_checked_in:
        for day_num in range(1, 11):
            checkin_field = f'day_{day_num}_checkin'
            checkout_field = f'day_{day_num}_checkout'
            checkin_time = getattr(vr, checkin_field)
            checkout_time = getattr(vr, checkout_field)
            if checkin_time and not checkout_time:
                checked_in_visitors.append({
                    'visit': vr,
                    'day_num': day_num,
                    'checkin_time': checkin_time
                })
                break  # Only add once per visit

    context = {
        'pending_requests': pending_requests,
        'approved_requests': approved_requests,
        'rejected_requests': rejected_requests,
        'frequent_visitors': frequent_visitors,
        'user': request.user,
        'all_visits': all_visits,
        'checked_in_visitors': checked_in_visitors,
    }
    return render(request, 'hr_dashboard.html', context)

@login_required(login_url='/hos-login/')
def hos_dashboard_view(request):
    if not is_hos_user(request.user):
        logout(request)
        messages.error(request, 'You do not have permission to access this portal.')
        return redirect('hos-login')
    now = timezone.now()
    today = now.date()
    seven_days_ago = now - timedelta(days=7)
    pending_requests = VisitRequest.objects.filter(status='PENDING', host__user_type='HOS', created_at__gte=seven_days_ago).order_by('-created_at')
    approved_requests = VisitRequest.objects.filter(status='APPROVED', host__user_type='HOS', created_at__gte=seven_days_ago).order_by('-created_at')
    rejected_requests = VisitRequest.objects.filter(status='REJECTED', host__user_type='HOS', created_at__gte=seven_days_ago).order_by('-created_at')
    all_visits = VisitRequest.objects.select_related('visitor').filter(host__user_type='HOS', created_at__gte=seven_days_ago)
    all_visits_for_checked_in = VisitRequest.objects.select_related('visitor').filter(host__user_type='HOS')
    recent_visitors = Visitor.objects.filter(visitrequest__host__user_type='HOS', visitrequest__created_at__gte=seven_days_ago)
    frequent_visitors = (
        recent_visitors.annotate(
            num_visits=Count('visitrequest', filter=Q(visitrequest__created_at__gte=seven_days_ago, visitrequest__host__user_type='HOS')),
            last_visit=Max('visitrequest__created_at', filter=Q(visitrequest__created_at__gte=seven_days_ago, visitrequest__host__user_type='HOS'))
        )
        .filter(num_visits__gt=0)
        .order_by('-num_visits', '-last_visit')[:10]
    )
    # Checked-in visitors for any day (multi-day aware, no 7-day filter)
    checked_in_visitors = []
    for vr in all_visits_for_checked_in:
        for day_num in range(1, 11):
            checkin_field = f'day_{day_num}_checkin'
            checkout_field = f'day_{day_num}_checkout'
            checkin_time = getattr(vr, checkin_field)
            checkout_time = getattr(vr, checkout_field)
            if checkin_time and not checkout_time:
                checked_in_visitors.append({
                    'visit': vr,
                    'day_num': day_num,
                    'checkin_time': checkin_time
                })
                break  # Only add once per visit
    context = {
        'pending_requests': pending_requests,
        'approved_requests': approved_requests,
        'rejected_requests': rejected_requests,
        'frequent_visitors': frequent_visitors,
        'user': request.user,
        'all_visits': all_visits,
        'checked_in_visitors': checked_in_visitors,
    }
    return render(request, 'hos_dashboard.html', context)

@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def search_visitors(request):
    # Check if user is authenticated and is a registration user
    if not request.user.is_authenticated or not is_registration_user(request.user):
        return JsonResponse({'error': 'Authentication required'}, status=401)
    
    query = request.GET.get('q', '').strip()
    results = []
    if query:
        visitors = Visitor.objects.filter(
            models.Q(first_name__icontains=query) |
            models.Q(last_name__icontains=query) |
            models.Q(email__icontains=query) |
            models.Q(phone__icontains=query) |
            models.Q(id_proof_number__icontains=query)
        )[:10]
        for v in visitors:
            results.append({
                'id': v.id,
                'first_name': v.first_name,
                'last_name': v.last_name,
                'email': v.email,
                'phone': v.phone,
                'company': v.company,
                'id_proof_type': v.id_proof_type,
                'id_proof_number': v.id_proof_number,
            })
    return JsonResponse({'results': results})

# @login_required(login_url='/login/')
# def update_request_status(request, request_id, action):
#     if request.method == 'POST':
#         visit_request = get_object_or_404(VisitRequest, id=request_id)
#         visitor = visit_request.visitor
#         visitor_email = visitor.email
#         visitor_name = f"{visitor.first_name} {visitor.last_name}" if visitor.first_name or visitor.last_name else "Visitor"
#         company_name = "Godrej"
#         email_sent = False
#         if action.upper() == 'APPROVE':
#             visit_request.status = 'APPROVED'
#             subject = f"Your visit to {company_name} has been approved"
#             message = f"Dear {visitor_name}, your visit request has been approved. Please carry your ID and visit as per your appointment."
#         elif action.upper() == 'REJECT':
#             visit_request.status = 'REJECTED'
#             subject = f"Your visit to {company_name} has been rejected"
#             message = f"Dear {visitor_name}, unfortunately your visit request has been rejected. Please contact HR for more details."
#         else:
#             subject = message = None
#         visit_request.save()
from django.utils import timezone

@login_required(login_url='/login/')
def update_request_status(request, request_id, action):
    if request.method == 'POST':
        visit_request = get_object_or_404(VisitRequest, id=request_id)
        visitor = visit_request.visitor
        visitor_email = visitor.email
        visitor_name = f"{visitor.first_name} {visitor.last_name}" if visitor.first_name or visitor.last_name else "Visitor"
        company_name = "Godrej"
        email_sent = False

        if action.upper() == 'APPROVE':
            visit_request.status = 'APPROVED'
            visit_request.approved_at = timezone.now()   # ✅ fix
            subject = f"Your visit to {company_name} has been approved"
            message = f"Dear {visitor_name}, your visit request has been approved. Please carry your ID and visit as per your appointment."

        elif action.upper() == 'REJECT':
            visit_request.status = 'REJECTED'
            visit_request.rejected_at = timezone.now()   # ✅ fix
            subject = f"Your visit to {company_name} has been rejected"
            message = f"Dear {visitor_name}, unfortunately your visit request has been rejected. Please contact HR for more details."

        else:
            subject = message = None

        visit_request.save()




        # Send email if possible
        if visitor_email and subject and message:
            try:
                send_mail(
                    subject,
                    message,
                    f"Godrej Visitor Management System <{settings.EMAIL_HOST_USER}>",
                    [visitor_email],
                    fail_silently=False,
                )
                email_sent = True
            except Exception as e:
                logging.error(f"Failed to send email to visitor: {visitor_email}. Error: {e}")
        # Optionally, you could log email_sent or show a message
    user_type = getattr(request.user, 'user_type', None)
    if user_type == 'HOS':
        return redirect('hos-dashboard')
    else:
        return redirect('hr-dashboard')

@login_required(login_url='/login/')
def export_visitors_excel(request):
    def get_user_display_name(user):
        if not user:
            return ""
        if user.first_name or user.last_name:
            return f"{user.first_name or ''} {user.last_name or ''}".strip()
        return user.username or user.email or str(user)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Visitors'
    # Header with day-specific columns
    header = [
        'Visitor ID', 'First Name', 'Last Name', 'Email', 'Phone', 'Organization',
        'ID Proof Type', 'ID Proof Number', 'Photo URL',
        "Reference Employee Name", "Reference Employee Department", "Reference Purpose",
        'Start Time', 'End Time', 'Valid Upto',
        'Visitor Card ID', 'Approved By', 'Approver Type'
    ]
    
    # Add day-specific check-in/check-out columns (10 days)
    for day in range(1, 11):
        header.extend([f'Day {day} Check In', f'Day {day} Check Out'])
    
    ws.append(header)
    visit_requests = VisitRequest.objects.select_related('visitor', 'approved_by', 'host').prefetch_related('visitorcard').all()
    if not visit_requests.exists():
        ws.append(['No data found'] + [''] * (len(header) - 1))
    else:
        for vr in visit_requests:
            v = vr.visitor
            # Get visitor card ID if exists
            visitor_card_id = ''
            try:
                visitor_card = vr.visitorcard
                visitor_card_id = visitor_card.card_number if visitor_card else ''
            except:
                visitor_card_id = ''
            
            # Get approver information
            approver_name = ''
            approver_type = ''
            if vr.approved_by:
                approver_name = get_user_display_name(vr.approved_by)
                approver_type = vr.approved_by.user_type
            elif vr.host:
                approver_name = get_user_display_name(vr.host)
                approver_type = vr.host.user_type
            
            # Base row data
            row_data = [
                v.id, v.first_name, v.last_name, v.email, v.phone, v.company,
                v.id_proof_type, v.id_proof_number,
                v.photo.url if v.photo else '',
                vr.reference_employee_name or '',
                vr.reference_employee_department or '',
                vr.reference_purpose or '',
                vr.start_time.strftime('%Y-%m-%d %H:%M') if vr.start_time else '',
                vr.end_time.strftime('%H:%M') if vr.end_time else '',
                vr.valid_upto.strftime('%Y-%m-%d') if vr.valid_upto else '',
                visitor_card_id,
                approver_name,
                approver_type
            ]
            
            # Add day-specific check-in/check-out data
            for day in range(1, 11):
                checkin_field = f'day_{day}_checkin'
                checkout_field = f'day_{day}_checkout'
                
                checkin_time = getattr(vr, checkin_field)
                checkout_time = getattr(vr, checkout_field)
                
                row_data.append(localtime(checkin_time).strftime('%Y-%m-%d %H:%M:%S') if checkin_time else '')
                row_data.append(localtime(checkout_time).strftime('%Y-%m-%d %H:%M:%S') if checkout_time else '')
            
            ws.append(row_data)
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=visitors.xlsx'
    wb.save(response)
    return response

@login_required(login_url='/hos-login/')
def export_hos_visitors_excel(request):
    if not is_hos_user(request.user):
        return HttpResponse('Access denied', status=403)

    def get_user_display_name(user):
        if not user:
            return ""
        if user.first_name or user.last_name:
            return f"{user.first_name or ''} {user.last_name or ''}".strip()
        return user.username or user.email or str(user)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'HOS Visitors'
    # Header with day-specific columns
    header = [
        'Visitor ID', 'First Name', 'Last Name', 'Email', 'Phone', 'Organization',
        'ID Proof Type', 'ID Proof Number', 'Photo URL',
        "Reference Employee Name", "Reference Employee Department", "Reference Purpose",
        'Start Time', 'End Time', 'Valid Upto',
        'Visitor Card ID', 'Approved By', 'Approver Type'
    ]
    
    # Add day-specific check-in/check-out columns (10 days)
    for day in range(1, 11):
        header.extend([f'Day {day} Check In', f'Day {day} Check Out'])
    
    ws.append(header)
    # Only export visits where HOS is the host
    visit_requests = VisitRequest.objects.select_related('visitor', 'approved_by', 'host').prefetch_related('visitorcard').filter(host__user_type='HOS')
    if not visit_requests.exists():
        ws.append(['No data found'] + [''] * (len(header) - 1))
    else:
        for vr in visit_requests:
            v = vr.visitor
            # Get visitor card ID if exists
            visitor_card_id = ''
            try:
                visitor_card = vr.visitorcard
                visitor_card_id = visitor_card.card_number if visitor_card else ''
            except:
                visitor_card_id = ''

            # Get approver information
            approver_name = ''
            approver_type = ''
            if vr.approved_by:
                approver_name = get_user_display_name(vr.approved_by)
                approver_type = vr.approved_by.user_type
            elif vr.host:
                approver_name = get_user_display_name(vr.host)
                approver_type = vr.host.user_type

            # Base row data
            row_data = [
                v.id, v.first_name, v.last_name, v.email, v.phone, v.company,
                v.id_proof_type, v.id_proof_number,
                v.photo.url if v.photo else '',
                vr.reference_employee_name or '',
                vr.reference_employee_department or '',
                vr.reference_purpose or '',
                vr.start_time.strftime('%Y-%m-%d %H:%M') if vr.start_time else '',
                vr.end_time.strftime('%H:%M') if vr.end_time else '',
                vr.valid_upto.strftime('%Y-%m-%d') if vr.valid_upto else '',
                visitor_card_id,
                approver_name,
                approver_type
            ]
            
            # Add day-specific check-in/check-out data
            for day in range(1, 11):
                checkin_field = f'day_{day}_checkin'
                checkout_field = f'day_{day}_checkout'
                
                checkin_time = getattr(vr, checkin_field)
                checkout_time = getattr(vr, checkout_field)
                
                row_data.append(localtime(checkin_time).strftime('%Y-%m-%d %H:%M:%S') if checkin_time else '')
                row_data.append(localtime(checkout_time).strftime('%Y-%m-%d %H:%M:%S') if checkout_time else '')
            
            ws.append(row_data)
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=hos_visitors.xlsx'
    wb.save(response)
    return response

@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def ajax_password_reset(request):
    email = request.data.get('email') or request.POST.get('email')
    if not email:
        return Response({'success': False, 'title': 'Invalid Email', 'message': 'Please enter your email address.', 'icon': '❌'}, status=400)
    UserModel = HRUser
    try:
        user = UserModel.objects.get(email=email)
    except UserModel.DoesNotExist:
        return Response({'success': False, 'title': 'Invalid Email', 'message': 'Invalid or unregistered email address.', 'icon': '❌'}, status=404)
    # Send password reset email using Django's logic
    form = PasswordResetForm({'email': email})
    if form.is_valid():
        form.save(
            request=request._request if hasattr(request, '_request') else request,
            use_https=request.is_secure(),
            email_template_name='registration/password_reset_email.html',
            subject_template_name='registration/password_reset_subject.txt',
            from_email=settings.DEFAULT_FROM_EMAIL,
        )
        return Response({'success': True, 'title': 'Email Sent', 'message': 'Password reset link sent to your email.', 'icon': '✅'})
    else:
        return Response({'success': False, 'title': 'Invalid Email', 'message': 'Invalid or unregistered email address.', 'icon': '❌'}, status=404)

logger = logging.getLogger(__name__)

@login_required(login_url='/login/')
@user_passes_test(is_hr_user)
def registration_users_list(request):
    users = HRUser.objects.filter(user_type='REGISTRATION').values('id', 'username', 'email', 'is_active')
    logger.debug(f"Listing registration users: {list(users)}")
    print('DEBUG: Registration users returned:', list(users))
    return JsonResponse({'users': list(users)})

@login_required(login_url='/login/')
@user_passes_test(is_hr_user)
@require_POST
def add_registration_user(request):
    import json
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body)
            username = data.get('username')
            password = data.get('password')
            email = data.get('email')
        except Exception as e:
            return JsonResponse({'error': 'Invalid JSON.'}, status=400)
    else:
        username = request.POST.get('username')
        password = request.POST.get('password')
        email = request.POST.get('email')
    logger.debug(f"Attempting to add registration user: {username}, {email}")
    if not username or not password:
        logger.error("Username and password are required.")
        return JsonResponse({'error': 'Username and password are required.'}, status=400)
    if HRUser.objects.filter(username=username).exists():
        logger.error("Username already exists.")
        return JsonResponse({'error': 'Username already exists.'}, status=400)
    try:
        # Auto-generate a unique employee_id for registration users
        employee_id = None
        for _ in range(10):  # Try up to 10 times
            candidate = 'REG-' + ''.join(random.choices(string.digits, k=6))
            if not HRUser.objects.filter(employee_id=candidate).exists():
                employee_id = candidate
                break
        if not employee_id:
            return JsonResponse({'error': 'Could not generate unique employee ID.'}, status=500)
        user = HRUser.objects.create_user(
            username=username,
            password=password,
            email=email,
            user_type='REGISTRATION',
            is_active=True,
            employee_id=employee_id
        )
        logger.info(f"Created registration user: {user.username} (id={user.id})")
        return JsonResponse({'success': True, 'user': {'id': user.id, 'username': user.username, 'email': user.email}})
    except Exception as e:
        logger.error(f"Error creating registration user: {e}")
        return JsonResponse({'error': str(e)}, status=500)

@login_required(login_url='/login/')
@user_passes_test(is_hr_user)
@require_POST
def delete_registration_user(request):
    user_id = request.POST.get('user_id')
    try:
        user = HRUser.objects.get(id=user_id, user_type='REGISTRATION')
        user.delete()
        return JsonResponse({'success': True})
    except HRUser.DoesNotExist:
        return JsonResponse({'error': 'User not found.'}, status=404)

@csrf_protect
def hos_password_reset(request):
    if request.method == 'POST':
        form = PasswordResetForm(request.POST)
        if form.is_valid():
            form.save(
                request=request,
                use_https=request.is_secure(),
                email_template_name='registration/password_reset_email.html',
                subject_template_name='registration/password_reset_subject.txt',
            )
            return redirect('hos-password-reset-done')
    else:
        form = PasswordResetForm()
    return render(request, 'hos_password_reset.html', {'form': form})

def hos_password_reset_done(request):
    return render(request, 'hos_password_reset_done.html')

@csrf_protect
def hos_password_reset_confirm(request, uidb64, token):
    UserModel = get_user_model()
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = UserModel.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, UserModel.DoesNotExist):
        user = None
    if user is not None and default_token_generator.check_token(user, token):
        if request.method == 'POST':
            form = SetPasswordForm(user, request.POST)
            if form.is_valid():
                form.save()
                return redirect('hos-password-reset-complete')
        else:
            form = SetPasswordForm(user)
        return render(request, 'hos_password_reset_confirm.html', {'form': form})
    else:
        messages.error(request, 'The password reset link is invalid or has expired.')
        return redirect('hos-login')

def hos_password_reset_complete(request):
    return render(request, 'hos_password_reset_complete.html')

@login_required(login_url='/login/')
def logout_view(request):
    # Store user type before logout since request.user will be None after logout
    user_type = getattr(request.user, 'user_type', None)
    logout(request)
    # Clear any session data to prevent conflicts
    request.session.flush()
    messages.success(request, 'You have been successfully logged out.')
    if user_type == 'REGISTRATION':
        return redirect('registration-login')
    elif user_type == 'HOS':
        return redirect('hos-login')
    else:
        return redirect('login')

def registration_logout_view(request):
    logout(request)
    # Clear any session data to prevent conflicts
    request.session.flush()
    return redirect('registration-login')

def hos_logout_view(request):
    logout(request)
    # Clear any session data to prevent conflicts
    request.session.flush()
    return redirect('hos-login')

@login_required(login_url='/login/')
@user_passes_test(is_hr_user)
def clear_all_sessions(request):
    """Clear all user sessions - Admin only function"""
    if request.method == 'POST':
        from django.contrib.sessions.models import Session
        session_count = Session.objects.count()
        Session.objects.all().delete()
        # Log out the current user and flush their session to avoid SessionInterrupted
        logout(request)
        request.session.flush()
        messages.success(request, f'Successfully cleared {session_count} user sessions. All users will need to log in again.')
        return redirect('login')
    else:
        return render(request, 'clear_sessions_confirm.html')

def print_card_dashboard(request):
    # Show all approved VisitRequests where the card is not printed or does not exist yet
    # Use select_related to optimize database queries and prevent visitor data issues
    import logging
    logger = logging.getLogger(__name__)
    
    approved_requests = VisitRequest.objects.select_related('visitor').filter(status='APPROVED').order_by('-created_at')
    requests_with_cards = []
    
    # Debug logging to track visitor data
    for req in approved_requests:
        logger.info(f"Processing request {req.id}: {req.visitor.first_name} {req.visitor.last_name} (Visitor ID: {req.visitor.id})")
        visitor_card = getattr(req, 'visitorcard', None)
        # Exclude if card exists and is printed
        if visitor_card and visitor_card.printed:
            continue
        requests_with_cards.append({
            'visit_request': req,
            'visitor_card': visitor_card
        })
    
    logger.info(f"Total requests to display: {len(requests_with_cards)}")
    return render(request, 'print_card_dashboard.html', {'requests_with_cards': requests_with_cards})

@require_POST
@csrf_protect
def delete_unprinted_visit_request(request, visit_id):
    try:
        visit = VisitRequest.objects.select_related('visitor').get(id=visit_id, status='APPROVED')
        # Only allow delete if no VisitorCard exists or if VisitorCard exists but is not printed
        if hasattr(visit, 'visitorcard') and visit.visitorcard.printed:
            return JsonResponse({'success': False, 'error': 'Card already printed for this visitor.'})
        # If VisitorCard exists but is not printed, delete it first
        if hasattr(visit, 'visitorcard'):
            visit.visitorcard.delete()
        visit.delete()
        return JsonResponse({'success': True})
    except VisitRequest.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Visitor request not found or already deleted.'})

def print_card_step2(request):
    """Step 2 of print card process - generate cards for selected visitors"""
    from visitorapi.models import VisitorCard, VisitRequest
    if request.method == 'POST':
        selected_visitor_ids = request.POST.getlist('selected_visitors')
        if not selected_visitor_ids:
            messages.error(request, 'No visitors selected for card printing.')
            return redirect('print_card_dashboard')
        visitor_card_ids = []
        for visit_id in selected_visitor_ids:
            visit_request = VisitRequest.objects.select_related('visitor').filter(id=visit_id, status='APPROVED').first()
            if not visit_request:
                continue
            visitor_card = VisitorCard.objects.filter(visit_request=visit_request).first()
            if not visitor_card:
                import random
                import string
                while True:
                    card_number = 'VC-' + ''.join(random.choices(string.digits, k=8))
                    if not VisitorCard.objects.filter(card_number=card_number).exists():
                        break
                visitor_card = VisitorCard(
                    visit_request=visit_request,
                    card_number=card_number,
                    issued_by=request.user if request.user.is_authenticated else None
                )
                visitor_card.save()
            visitor_card_ids.append(visitor_card.id)
        request.session['step2_visitor_card_ids'] = visitor_card_ids
    else:
        visitor_card_ids = request.session.get('step2_visitor_card_ids', [])
        if not visitor_card_ids:
            return redirect('print_card_dashboard')
    generated_cards = VisitorCard.objects.select_related('visit_request__visitor').filter(id__in=visitor_card_ids, printed=False)
    if not generated_cards.exists():
        messages.error(request, 'No valid visitor cards found for printing.')
        return redirect('print_card_dashboard')
    return render(request, 'print_card_step2.html', {
        'generated_cards': generated_cards,
        'card_count': generated_cards.count()
    })

@require_POST
@csrf_protect
def mark_card_printed(request):
    try:
        import json
        from django.utils import timezone
        data = json.loads(request.body)
        visitor_id = data.get('visitor_id')
        card_number = data.get('card_number')
        if not visitor_id or not card_number:
            return JsonResponse({'success': False, 'error': 'Missing visitor_id or card_number'})
        visit_request = VisitRequest.objects.select_related('visitor').get(id=visitor_id, status='APPROVED')
        # Always set check-in time using day_1_checkin to current time
        visit_request.day_1_checkin = timezone.now()
        visit_request.save(update_fields=['day_1_checkin'])
        # Create or update visitor card
        visitor_card, created = VisitorCard.objects.get_or_create(
            visit_request=visit_request,
            defaults={
                'card_number': card_number,
                'issued_by': request.user,
                'issued_at': timezone.now()
            }
        )
        if not created:
            visitor_card.card_number = card_number
            visitor_card.issued_by = request.user
            visitor_card.issued_at = timezone.now()
            visitor_card.save()
        return JsonResponse({'success': True, 'message': f'Card {card_number} marked as printed'})
    except VisitRequest.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Visitor request not found'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@require_POST
@csrf_protect
def mark_cards_printed(request):
    import logging
    logger = logging.getLogger(__name__)
    logger.warning('mark_cards_printed endpoint called')
    if request.method == 'POST':
        import json
        data = json.loads(request.body)
        card_ids = data.get('card_ids', [])
        from visitorapi.models import VisitorCard
        logger.warning(f'Updating VisitorCards with ids: {card_ids}')
        updated = VisitorCard.objects.filter(id__in=card_ids).update(printed=True)
        # Set check-in time for all related VisitRequests using day_1_checkin
        visitor_cards = VisitorCard.objects.select_related('visit_request').filter(id__in=card_ids)
        for card in visitor_cards:
            visit_request = card.visit_request
            # Use day_1_checkin for initial check-in when card is printed
            if not visit_request.day_1_checkin:
                visit_request.day_1_checkin = timezone.now()
                visit_request.save(update_fields=['day_1_checkin'])
        print('MARK PRINTED: card_ids sent:', card_ids, 'records updated:', updated)
        if hasattr(request, 'session'):
            request.session.pop('step2_visitor_card_ids', None)
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)

@require_POST
@csrf_protect
def clear_print_session(request):
    """Clear session data for print card process"""
    if hasattr(request, 'session'):
        request.session.pop('step2_visitor_card_ids', None)
    return JsonResponse({'success': True})

@api_view(['POST'])
@permission_classes([AllowAny])
@parser_classes([MultiPartParser, FormParser])
def upload_visitor_photo(request, visitor_id):
    from .models import Visitor
    try:
        visitor = Visitor.objects.get(id=visitor_id)
        photo = request.FILES.get('photo')
        if not photo:
            return JsonResponse({'success': False, 'error': 'No photo uploaded'}, status=400)
        # Delete old photo if exists
        if visitor.photo:
            visitor.photo.delete(save=False)
        visitor.photo = photo
        visitor.save()
        return JsonResponse({'success': True, 'photo_url': visitor.photo.url})
    except Visitor.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Visitor not found'}, status=404)

@csrf_exempt
def checkout_visitor(request):
    from visitorapi.models import VisitorCard
    from django.utils import timezone
    from django.utils.timezone import localtime
    import json
    if request.method == 'POST':
        qr_data = request.POST.get('qr_data') or (json.loads(request.body).get('qr_data') if request.body else None)
        if not qr_data:
            return JsonResponse({'success': False, 'error': 'No QR data provided'})
        # Parse card_number from qr_data (split by |)
        card_number = qr_data.split('|')[0]
        try:
            card = VisitorCard.objects.select_related('visit_request__visitor').get(card_number=card_number)
            visit_request = card.visit_request
            
            # Check if visitor can check out today
            if not visit_request.can_check_out_today():
                return JsonResponse({'success': False, 'error': 'Cannot check out. Either not checked in today or already checked out.'})
            
            # Get today's checkout field
            checkout_field = visit_request.get_today_checkout_field()
            if not checkout_field:
                return JsonResponse({'success': False, 'error': 'Invalid visit period.'})
            
            # Set checkout time for today
            setattr(visit_request, checkout_field, timezone.now())
            visit_request.save(update_fields=[checkout_field])
            checkout_time_ist = localtime(getattr(visit_request, checkout_field)).strftime('%Y-%m-%d %H:%M:%S')
            return JsonResponse({
                'success': True, 
                'message': 'Checked out!', 
                'checkout_time': checkout_time_ist
            })
        except VisitorCard.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Card not found'})
    return JsonResponse({'success': False, 'error': 'Invalid request'})

@csrf_exempt
def checkin_visitor(request):
    from visitorapi.models import VisitorCard
    from django.utils import timezone
    from django.utils.timezone import localtime
    import json
    if request.method == 'POST':
        qr_data = request.POST.get('qr_data') or (json.loads(request.body).get('qr_data') if request.body else None)
        if not qr_data:
            return JsonResponse({'success': False, 'error': 'No QR data provided'})
        # Parse card_number from qr_data (split by |)
        card_number = qr_data.split('|')[0]
        try:
            card = VisitorCard.objects.select_related('visit_request__visitor').get(card_number=card_number)
            visit_request = card.visit_request
            # Block check-in if today is after valid_upto
            from datetime import date
            if visit_request.valid_upto and date.today() > visit_request.valid_upto:
                return JsonResponse({'success': False, 'error': 'Your visit date is finished.'})
            # Check if visitor can check in today
            if not visit_request.can_check_in_today():
                return JsonResponse({'success': False, 'error': 'Cannot check in. Visit period is not valid for today.'})
            # Get today's checkin field
            checkin_field = visit_request.get_today_checkin_field()
            if not checkin_field:
                return JsonResponse({'success': False, 'error': 'Invalid visit period.'})
            # Check if already checked in today
            if getattr(visit_request, checkin_field) is not None:
                return JsonResponse({'success': False, 'error': 'Already checked in today.'})
            # Set checkin time for today
            setattr(visit_request, checkin_field, timezone.now())
            visit_request.save(update_fields=[checkin_field])
            checkin_time_ist = localtime(getattr(visit_request, checkin_field)).strftime('%Y-%m-%d %H:%M:%S')
            return JsonResponse({
                'success': True, 
                'message': 'Checked in!', 
                'checkin_time': checkin_time_ist
            })
        except VisitorCard.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Card not found'})
    return JsonResponse({'success': False, 'error': 'Invalid request'})

def checkout_page(request):
    return render(request, 'checkout.html')

@login_required(login_url='/login/')
def checked_in_visitors(request):
    from visitorapi.models import VisitRequest
    all_visits = VisitRequest.objects.select_related('visitor').all()
    data = []
    for vr in all_visits:
        for day_num in range(1, 11):
            checkin_field = f'day_{day_num}_checkin'
            checkout_field = f'day_{day_num}_checkout'
            checkin_time = getattr(vr, checkin_field)
            checkout_time = getattr(vr, checkout_field)
            if checkin_time and not checkout_time:
                data.append({
                    'id': vr.id,
                    'name': f"{vr.visitor.first_name} {vr.visitor.last_name}",
                    'company': vr.visitor.company,
                    'purpose': vr.purpose,
                    'checkin_time': checkin_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'day_num': day_num,
                })
                break  # Only add once per visit
    return JsonResponse({'visitors': data})

@require_POST
@login_required(login_url='/login/')
def manual_checkout_visitor(request):
    from django.utils import timezone
    from visitorapi.models import VisitRequest
    import json
    try:
        data = json.loads(request.body)
        visit_id = data.get('visit_id')
        if not visit_id:
            return JsonResponse({'success': False, 'error': 'No visit ID provided'})
        visit = VisitRequest.objects.select_related('visitor').get(id=visit_id)
        # Find the first day_X_checkin that is set and day_X_checkout is not set
        checked_out = False
        for day_num in range(1, 11):
            checkin_field = f'day_{day_num}_checkin'
            checkout_field = f'day_{day_num}_checkout'
            checkin_time = getattr(visit, checkin_field)
            checkout_time = getattr(visit, checkout_field)
            if checkin_time and not checkout_time:
                setattr(visit, checkout_field, timezone.now())
                visit.checkout_by_hr = True
                visit.save(update_fields=[checkout_field, 'checkout_by_hr'])
                checked_out = True
                return JsonResponse({'success': True, 'checkout_time': getattr(visit, checkout_field).strftime('%Y-%m-%d %H:%M:%S') + ' HR'})
        if not checked_out:
            return JsonResponse({'success': False, 'error': 'No active check-in found to check out.'})
    except VisitRequest.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Visit not found'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
