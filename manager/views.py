import json
import os
import requests
import datetime
from dotenv import load_dotenv
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.shortcuts import redirect, render, get_object_or_404
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.renderers import TemplateHTMLRenderer
from community.models import BookRequest, UserRequestBook, Inquiry
from audiobook.models import Book
from user.models import Subscription
from .serializers import BookSerializer, InquirySerializer
from community.views import send_async_mail
from datetime import datetime
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from rest_framework import status


load_dotenv()  # 환경 변수를 로드함


# 책 수요 변화

def book_view(request):
    return Response({'message': 'Good'})


def book_view_count(request):
    return Response({'message': 'Good'})


# 도서 신청 확인 페이지
def get_book_details_from_naver(isbn):

    # 캐시에서 데이터를 먼저 찾음
    cache_key = f'book_{isbn}'
    cached_data = cache.get(cache_key)
    if cached_data:
        return json.loads(cached_data)
    

    # 캐시에 데이터가 없으면 API 호출
    url = f'https://openapi.naver.com/v1/search/book.json?query={isbn}'
    headers = {
        "X-Naver-Client-Id": os.getenv('NAVER_CLIENT_ID'),
        "X-Naver-Client-Secret": os.getenv('NAVER_CLIENT_SECRET'),
    }

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        # isbn은 unique하므로, items의 첫번째 요소만 가져옴
        book_data = response.json().get('items')[0]
        data = {
            'author': book_data.get('author'),
            'title': book_data.get('title'),
            'publisher': book_data.get('publisher'),
            'image': book_data.get('image'),
            'isbn': book_data.get('isbn'),
            'description': book_data.get('description'),
        }

        # 데이터를 캐시에 저장
        data = json.dumps(data)  # json 형태로 직렬화
        cache.set(cache_key, data, timeout=86400 * 7)  # 1주일 동안 캐시 유지
        return json.loads(data)  # 역직렬화하여 반환
    else:
        return None


class BookRequestListView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'manager/book_request.html'

    def get(self, request):

        if not request.user.is_admin:
            return redirect('audiobook:main')

        book_requests = BookRequest.objects.all()
        book_list = []
        for book_request in book_requests:
            book_details = get_book_details_from_naver(
                book_request.request_isbn)
            if book_details:
                book_list.append({
                    'isbn': book_request.request_isbn,
                    'author': book_details['author'],
                    'title': book_details['title'],
                    'publisher': book_details['publisher'],
                    'request_count': book_request.request_count
                })

        book_list_sorted = sorted(
            book_list, key=lambda x: x['request_count'], reverse=True)

        # Paginator 설정
        paginator = Paginator(book_list_sorted, 10)
        page = request.GET.get('page')  # URL에서 페이지 번호 가져오기
        books = paginator.get_page(page)  # 해당 페이지의 책 가져오기
        context = {'book_list': books}

        return Response(context)


class BookRegisterView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'manager/book_register.html'

    def get(self, request, book_isbn):
        if not request.user.is_admin:
            return redirect('audiobook:main')

        book_details = get_book_details_from_naver(book_isbn)
        if book_details:
            return Response(book_details)
        else:
            return Response({"error": "book_datail이 존재하지 않습니다"})


class BookRegisterCompleteView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'manager/book_register_complete.html'

    def post(self, request):
        if not request.user.is_admin:
            return redirect('audiobook:main')

        # ISBN으로 이미 존재하는 책을 확인
        book_isbn = request.POST.get('book_isbn')

        try:
            existing_book = Book.objects.get(book_isbn=book_isbn)
            print("이미 ISBN이 존재합니다.")
            # 책이 이미 존재하면 에러 메시지와 함께 종료
            return Response({
                'status': 'error',
                'message': '이미 ISBN이 존재합니다.'
            }, status=400)
        except Book.DoesNotExist:
            # 책이 존재하지 않으면 처리를 계속
            pass

        # Naver API를 호출하여 책의 상세 정보를 가져옴
        book_details = get_book_details_from_naver(book_isbn)
        print(book_details)
        if book_details is None:
            print("There is no book details.")
            return Response({
                'status': 'error',
                'message': 'Book details not found.'
            }, status=404)

        # 데이터 저장소에 파일을 저장
        # Naver API로부터 받은 이미지 URL에서 이미지를 다운로드
        image_response = requests.get(book_details['image'])
        if image_response.status_code != 200:
            print(image_response.status_code)
            return Response({
                'status': 'error',
                'message': 'Failed to download book image.'
            }, status=400)

        content_file = request.FILES.get('book_content')
        if not content_file:
            print("No content file provided.")
            return Response({
                'status': 'error',
                'message': 'No content file provided.'
            }, status=400)

        # 가져온 상세 정보와 폼 데이터를 결합
        book_data = {
            'book_title': book_details['title'],
            'book_genre': request.POST.get('book_genre'),  # 사용자 입력
            # 'book_image_path': ,
            'book_author': book_details['author'],
            'book_publisher': book_details['publisher'],
            'book_publication_date': datetime.date.today(),
            # 'book_content_path': ,
            'book_description': book_details['description'],
            'book_likes': 0,
            'book_isbn': book_isbn,
            'user': request.user.user_id,
        }

        # Serializer를 통해 데이터 검증 및 저장
        serializer = BookSerializer(data=book_data)
        if serializer.is_valid():
            book_instance = serializer.save()
            # 이미지와 텍스트 파일을 모델 인스턴스에 저장
            # 옵션 save=False 한 후 .save() 해서 한번에 저장
            book_instance.book_image_path.save(
                f"{book_isbn}_image.jpg", ContentFile(image_response.content), save=False)
            book_instance.book_content_path.save(
                content_file.name, content_file, save=False)
            book_instance.save()

        else:
            print(serializer.errors)
            return Response({
                'status': 'error',
                'message': 'Registration failed.',
                'errors': serializer.errors
            }, status=400)

        # 이메일 보내기
        book_request = get_object_or_404(BookRequest, request_isbn=book_isbn)
        user_request_books = UserRequestBook.objects.filter(
            request=book_request)
        for user_request_book in user_request_books:
            user = user_request_book.user
            if user.email:
                try:
                    subject = '[오디 알림] 신청하신 책 등록 완료'
                    html_content = render_to_string(
                        'manager/email_template.html', {'nickname': user.nickname})
                    plain_message = strip_tags(html_content)
                    from_email = '오디 <wooyoung9654@gmail.com>'
                    send_async_mail(subject, plain_message,
                                    from_email, [user.email])
                    print('Email sent successfully')
                except Exception as e:
                    # 로그 기록, 오류 처리 등
                    print(f'Error sending email: {e}')

        # BookRequest, UserRequest 삭제
        book_request.delete()
        user_request_books.delete()

        return Response({
            'status': 'success',
            'message': 'Book registered successfully.'
        }, status=200)


# 문의 답변

def inquiry_list(request):  # 문의글 목록 페이지
    return render(request, 'manager/inquiry_list.html')

def inquiry_detail(request, pk):  # 문의글 상세 페이지
    inquiry = Inquiry.objects.get(pk=pk)
    return render(request, 'manager/inquiry_detail.html', {'inquiry': inquiry})

class InquiryListAPI(APIView):

    def get(self, request, *args, **kwargs):
        show_answered = request.query_params.get('show_answered', 'all')
        if show_answered == 'answered':
            inquiries = Inquiry.objects.filter(inquiry_is_answered=True)
        elif show_answered == 'not_answered':
            inquiries = Inquiry.objects.filter(inquiry_is_answered=False)
        else:
            inquiries = Inquiry.objects.all()

        # 여러 인스턴스 직렬화
        serializer = InquirySerializer(inquiries, many=True)
        
        return Response(serializer.data)

class InquiryDetailAPI(APIView):
    
    def get_object(self, pk):
        try:
            return Inquiry.objects.get(pk=pk)
        except Inquiry.DoesNotExist:
            raise status.HTTP_404_NOT_FOUND

    def get(self, request, pk, format=None):
        inquiry = self.get_object(pk)
        serializer = InquirySerializer(inquiry)
        return Response(serializer.data)



# 구독 및 수익 관리

def show_subscription(request):
    if not request.user.is_admin:
            return redirect('audiobook:main')
        
    return render(request, 'manager/subscription.html')
    
class SubscriptionCountAPI(APIView):
    def get(self, request, format=None):
        today = timezone.now().date()  # 'aware' 현재 날짜 객체
        dates = [today - relativedelta(months=n) for n in range(11, -1, -1)]
        
        data = {
            'dates': [],
            'counts': []
        }
        for date_point in dates:
            # 날짜를 'aware' datetime 객체로 변환
            aware_date_point = timezone.make_aware(datetime.combine(date_point, datetime.min.time()))
            
            count = Subscription.objects.filter(
                sub_start_date__lte=aware_date_point,
                sub_end_date__gte=aware_date_point
            ).count()
            data['dates'].append(aware_date_point.strftime('%Y-%m'))
            data['counts'].append(count)
        print(data)
        
        return Response(data)
    
    


# FAQ 관리


def faq(request):
    return Response({'message': 'Good'})
