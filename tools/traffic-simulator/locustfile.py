from locust import HttpUser, task, between


class SmartLoadUser(HttpUser):

    wait_time = between(0.1, 1)

    @task
    def send_request(self):
        self.client.get("/")
