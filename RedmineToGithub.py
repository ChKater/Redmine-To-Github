import github3
import redminelib
from redminelib import Redmine
import time
import json, ast
import os
import re


class RedmineToGithub:

    def __init__(self, redmine_token, redmine_url, redmine_project, gitlab_token, gitlab_user, gitlab_project,
                 gitlab_prefix, gitlab_user_mapping, footer):
        self.redmine_token = redmine_token
        self.redmine_url = redmine_url
        self.redmine_project = redmine_project
        self.gitlab_token = gitlab_token
        self.gitlab_user = gitlab_user
        self.gitlab_project = gitlab_project
        self.gitlab_time_format = '%d %B %Y %H:%M UTC'
        self.gitlab_prefix = gitlab_prefix
        self.gitlab_user_mapping = gitlab_user_mapping
        self.getlab_next_issue_number = 0
        self.footer = footer
        self.__init_redmine__()
        self.__init_gitlab__()

    def __init_redmine__(self):
        self.redmine = Redmine(self.redmine_url, key=self.redmine_token)
        self.redmine_project = self.redmine.project.get(self.redmine_project)

    def __init_gitlab__(self):
        gh = github3.login(token=self.gitlab_token)
        self.gitlab_repositotry = gh.repository(self.gitlab_user, self.gitlab_project)
        self.gitlab_milestones = {}
        self.gitlab_issues = []
        for issue in self.gitlab_repositotry.issues(state='all'):
            self.gitlab_issues.append(issue)
            if issue.number > self.getlab_next_issue_number:
                self.getlab_next_issue_number = issue.number
        self.getlab_next_issue_number = self.getlab_next_issue_number + 1
        self.gitlab_milestone_list = self.gitlab_repositotry.milestones()

    def gitlab_create_milestones(self, name):
        for milestone in self.gitlab_milestone_list:
            if milestone.title == name:
                self.gitlab_milestones[name] = milestone.number
                return
        milestone = self.gitlab_repositotry.create_milestone(title=name)
        self.gitlab_milestones[name] = milestone.number

    def gitlab_milestone_exist(self, name):
        for milestone in self.gitlab_repositotry.milestones():
            if milestone.title == name:
                return True
        return False

    def gitlab_issue_exist(self, issue_name):
        for issue in self.gitlab_issues:
            if issue.title == issue_name:
                return issue
        return None

    def add_gitlab_issue(self, issue):
        return self.gitlab_repositotry.create_issue(**issue)

    def execute(self):
        print("Fetching issues from redmine")
        issues = self.redmine.issue.filter(project_id=self.redmine_project.id, limit=1000, status_id='*',
                                           include=['journals'], sort='id:asc')

        self.id_map = {}
        issues_to_process = []

        for issue in issues:
            gitlab_issue = self.gitlab_issue_exist(issue.subject)
            if gitlab_issue is not None:
                self.id_map[issue.id] = gitlab_issue.number
                continue
            self.id_map[issue.id] = self.getlab_next_issue_number
            self.getlab_next_issue_number = self.getlab_next_issue_number + 1
            issues_to_process.append(issue)

        for issue in issues_to_process:
            print("Processing issue #" + str(issue.id) + ": " + issue.subject)
            if hasattr(issue, 'fixed_version'):
                self.gitlab_create_milestones(issue.fixed_version.name)
            gitlab_issue_data = self.generate_gitlab_issue(issue.subject, issue.description, issue.author,
                                                           issue.created_on,
                                                           issue.fixed_version.name if hasattr(issue, 'fixed_version')
                                                           else "", issue.tracker.name,
                                                           issue.status.name,
                                                           str(issue.assigned_to.name) if hasattr(issue, 'assigned_to')
                                                           else "")
            attached_header_added = False;
            for journal in issue.journals:
                for detailJSON in journal.details:
                    detail = ast.literal_eval(json.dumps(detailJSON))
                    if 'property' in detail:
                        if detail['property'] == "attachment":
                            try:
                                attachment = self.redmine.attachment.get(detail['name'])
                            except redminelib.exceptions.ResourceNotFoundError:
                                continue
                            if not attached_header_added:
                                self.gitlabe_issue_add_attached_files_header(gitlab_issue_data)
                                attached_header_added = True
                            self.gitlab_issue_add_attachment(gitlab_issue_data, attachment.filename,
                                                             attachment.content_url)
                            self.download_file(detail['name'], attachment.filename, attachment.content_url)

            self.gitlabe_issue_add_migration(gitlab_issue_data, issue.id)
            gitlab_issue = self.add_gitlab_issue(gitlab_issue_data)
            self.id_map[issue.id] = gitlab_issue.number

            for journal in issue.journals:
                self.gitlab_issue_add_comment(gitlab_issue, journal.notes, journal.user, journal.created_on)
            if issue.status.name == "Closed" or issue.status.name == "Rejected" or issue.status.name == "Feedback" or issue.status.name == "Resolved":
                gitlab_issue.close()
            # prevent trigering abuse ddetection
            time.sleep(0.25)

    def generate_gitlab_issue(self, subject, description, author, creation_date, milestone, tracker, status, assigne):
        issue_data = {}
        issue_data['title'] = subject
        issue_data['body'] = '<b>Reported by ' + author.name + ' on ' + creation_date.strftime(
            self.gitlab_time_format) + '</b><br/>' + self.replace_hashtags(description)
        issue_data['labels'] = []
        issue_data['labels'].append(self.redmine_project)
        if tracker == "Bug":
            issue_data['labels'].append("bug")
        if status == "Rejected":
            issue_data['labels'].append("wontfix")

        if len(milestone) > 0:
            issue_data['milestone'] = self.gitlab_milestones[milestone]
        if len(assigne) > 0 and assigne in self.gitlab_user_mapping:
            issue_data['assignee'] = self.gitlab_user_mapping[assigne]
        return issue_data

    def gitlabe_issue_add_attached_files_header(self, issue_data):
        issue_data['body'] += '<hr><b>Attached Files: </b><br/>.\r\n\r\n'

    def gitlab_issue_add_attachment(self, issue_data, filename, file_url):
        issue_data['body'] += '* <a href="' + file_url.replace(self.redmine_url,
                                                               self.gitlab_prefix + "tree/master/") + '">' + filename + '</a> \r\n'

    def gitlabe_issue_add_migration(self, issue_data, id):
        issue_data['body'] += '<hr>' + self.footer

    def gitlab_issue_add_comment(self, issue, notes, author, creation_date):
        if len(notes) > 0:
            issue.create_comment('<b>Comment by ' + author.name + ' on ' + creation_date.strftime(
                self.gitlab_time_format) + '</b><br/>' + self.replace_hashtags(notes))

    def download_file(self, issue_id, file_name, file_url):
        save_path = "attachments/download/" + str(issue_id)
        if not os.path.exists(save_path):
            os.makedirs(save_path)
        self.redmine.download(url=file_url.replace("http:", "https:"), savepath=save_path, filename=file_name)

    def replace_hashtags(self, text):
        new_text = text
        for i in re.findall(r'(?i)(?<=\#)\w+', text):
            if i.isdigit():
                key = int(i)
                if key in self.id_map:
                    new_text = new_text.replace("#" + i, '<a href="' + self.gitlab_prefix + '/issues/' + str(
                        self.id_map[key]) + '">#' + str(self.id_map[key]) + '</a>')
        return new_text


if __name__ == '__main__':
    redmine_token = "redmine"
    redmine_url = "url to redmine website (without ending /)"
    redmine_project = "redmine project name"
    gitlab_token = "github access token"
    gitlab_user = "github user"
    gitlab_project = "github Project"
    gitlab_prefix = "url to github project (without ending /)"
    gitlab_user_mapping = {}
    gitlab_user_mapping['Max Mustermann'] = 'Gitlab username of Max Mustermann'
    

    footer = "Migrated from the Whatever redmine repository."
    r2g = RedmineToGithub(redmine_token, redmine_url, redmine_project, gitlab_token, gitlab_user, gitlab_project,
                          gitlab_prefix, gitlab_user_mapping, footer)
    r2g.execute()
